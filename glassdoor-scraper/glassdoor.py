"""
This is an example web scraper for Glassdoor.com used in scrapfly blog article:
https://scrapfly.io/blog/how-to-scrape-glassdoor/

To run this scraper set env variable $SCRAPFLY_KEY with your scrapfly API key:
$ export $SCRAPFLY_KEY="your key from https://scrapfly.io/dashboard"
"""
from enum import Enum
import json
import os
import re
from typing import Dict, List, Optional, Tuple, TypedDict
from urllib.parse import urljoin

from loguru import logger as log
from scrapfly import ScrapeApiResponse, ScrapeConfig, ScrapflyClient, ScrapflyScrapeError

SCRAPFLY = ScrapflyClient(key=os.environ["SCRAPFLY_KEY"])
BASE_CONFIG = {
    # Glassdoor.com requires Anti Scraping Protection bypass feature.
    # for more: https://scrapfly.io/docs/scrape-api/anti-scraping-protection
    "asp": True,
    "country": "US",
    "render_js": True,
    # Wait for page to fully load (may help bypass JS challenges)
    "rendering_wait": 5000,
    # Use session to maintain cookies which may help bypass login walls
    "session": "glassdoor_session",
    # Uncomment to use residential proxies (costs more API credits but may help with blocks)
    # "proxy_pool": "public_residential_pool",
}


def find_hidden_data(result: ScrapeApiResponse) -> Optional[dict]:
    """
    Extract hidden web cache (Apollo Graphql framework) from Glassdoor page HTML
    It's either in NEXT_DATA script or direct apolloState js variable
    """
    # data can be in __NEXT_DATA__ cache
    data = result.selector.css("script#__NEXT_DATA__::text").get()
    if data:
        data = json.loads(data)["props"]["pageProps"]["apolloCache"]
    else:
        match = re.search(r'apolloState":\s*({.+})};', result.content)
        if match:
            data = json.loads(match.group(1))
        else:
            log.warning(f"Could not find __NEXT_DATA__ or apolloState on page {result.context['url']}")
            return None

    def _unpack_apollo_data(apollo_data):
        """
        Glassdoor uses Apollo GraphQL client and the dataset is a graph of references.
        This function unpacks the __ref references to actual values.
        """

        def resolve_refs(data, root):
            if isinstance(data, dict):
                if "__ref" in data:
                    return resolve_refs(root[data["__ref"]], root)
                else:
                    return {k: resolve_refs(v, root) for k, v in data.items()}
            elif isinstance(data, list):
                return [resolve_refs(i, root) for i in data]
            else:
                return data

        if not apollo_data:
            return {}
        return resolve_refs(apollo_data.get("ROOT_QUERY") or apollo_data, apollo_data)

    return _unpack_apollo_data(data)


def parse_jobs(result: ScrapeApiResponse) -> Tuple[List[Dict], List[str]]:
    """Parse Glassdoor jobs page for job data and other page pagination urls"""
    selector = result.selector
    job_data = []
    for box in selector.xpath("//div[contains(@class, 'jobCard JobCard')]"):
        job_data.append({
            "jobTitle": box.xpath(".//a/text()").get(),
            "jobLink": box.xpath(".//a/@href").get(),
            "job_location": box.xpath(".//div[@data-test='emp-location']/text()").get(),
            "jobSalary": box.xpath(".//div[@data-test='detailSalary']/text()").get(),
            "jobDate": box.xpath("//div[@data-test='job-age']/text()").get(),
        })

    script_data = selector.xpath("//script[contains(text(), 'paginationLinks')]/text()").get()
    pagination_links = re.search(r'\\"paginationLinks\\":\s*(\[.*?\])\s*,\s*\\"searchResultsMetadata\\"', script_data).group(1)
    unescaped = pagination_links.replace('\\"', '"').replace('\\u0026', '&')
    pagination_links = json.loads(unescaped)
    
    other_pages = [
        urljoin(result.context["url"], page["urlLink"])
        for page in pagination_links
        if page["isCurrentPage"] is False
    ]
    
    return job_data, other_pages


async def scrape_jobs(url: str, max_pages: Optional[int] = None) -> List[Dict]:
    """Scrape Glassdoor job listing page for job listings (with pagination)"""
    log.info("scraping job listings from {}", url)
    first_page = await SCRAPFLY.async_scrape(ScrapeConfig(url, **BASE_CONFIG))

    jobs, other_page_urls = parse_jobs(first_page)
    _total_pages = len(other_page_urls) + 1
    if max_pages and _total_pages > max_pages:
        other_page_urls = other_page_urls[:max_pages]

    log.info("scraped first page of jobs of {}, scraping remaining {} pages", url, _total_pages - 1)
    other_pages = [ScrapeConfig(url, **BASE_CONFIG) for url in other_page_urls]
    async for result in SCRAPFLY.concurrent_scrape(other_pages):
        if not isinstance(result, ScrapflyScrapeError):
            jobs.extend(parse_jobs(result)[0])
        else:
            log.error(f"failed to scrape {result.api_response.config['url']}, got: {result.message}")
    log.info("scraped {} jobs from {} in {} pages", len(jobs), url, _total_pages)
    return jobs


def parse_reviews(result: ScrapeApiResponse) -> Dict:
    """parse Glassdoor reviews page for review data"""
    cache = find_hidden_data(result)
    if not cache:
        return {}
    reviews_data = next((v for k, v in cache.items() if k.startswith("employerReviewsRG")), {})
    return reviews_data


def parse_reviews_api_metadata(result: ScrapeApiResponse, debug_dir: Optional[str] = None) -> Dict:
    """parse Glassdoor reviews api metadata from html page"""
    selector = result.selector

    # Try multiple approaches to find employer metadata
    script_data = selector.xpath("//script[contains(text(), 'profileId')]/text()").get()

    if script_data:
        # Try to match employer object - handle nested braces
        employer_match = re.search(r'"employer"\s*:\s*(\{.*?"profileId"\s*:\s*\d+[^}]*\})', script_data)
        if employer_match:
            try:
                employer_metadata = json.loads(employer_match.group(1))
                return {
                    'employer_id': int(employer_metadata['id']),
                    'dynamic_profile_id': int(employer_metadata['profileId']),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        # Alternative: extract id and profileId directly
        id_match = re.search(r'"employer"\s*:\s*\{[^}]*"id"\s*:\s*(\d+)', script_data)
        profile_match = re.search(r'"profileId"\s*:\s*(\d+)', script_data)
        if id_match and profile_match:
            return {
                'employer_id': int(id_match.group(1)),
                'dynamic_profile_id': int(profile_match.group(1)),
            }

    # Fallback: try finding in Apollo state cache
    cache = find_hidden_data(result)
    if cache:
        for key, value in cache.items():
            if isinstance(value, dict):
                if 'id' in value and 'profileId' in value:
                    return {
                        'employer_id': int(value['id']),
                        'dynamic_profile_id': int(value['profileId']),
                    }

    # Save debug HTML if parsing fails
    if debug_dir:
        from pathlib import Path
        debug_path = Path(debug_dir) / "debug_page.html"
        debug_path.write_text(result.content, encoding='utf-8')
        log.warning(f"Saved debug HTML to {debug_path}")

    raise ValueError("Could not find employer metadata in page. The page structure may have changed.")


async def scrape_reviews(
    url: str,
    max_pages: Optional[int] = None,
    start_page: int = 1,
    output_file: Optional[str] = None,
    debug: bool = False
) -> List[Dict]:
    """
    Scrape Glassdoor reviews listings from reviews page (with pagination)

    Args:
        url: Glassdoor reviews page URL
        max_pages: Maximum number of pages to scrape (None for all)
        start_page: Page number to start from (for resuming interrupted scrapes)
        output_file: If provided, saves reviews incrementally to this JSON file
        debug: If True, saves HTML to results/debug_page.html when parsing fails

    Returns:
        List of review dictionaries
    """

    def generate_api_request_config(employer_id: int, dynamic_profile_id: int, page_number: int) -> ScrapeConfig:
        return ScrapeConfig(
            url='https://www.glassdoor.com/bff/employer-profile-mono/employer-reviews',
            method='POST',
            asp=True,
            country="US",
            session="glassdoor_session",  # maintain cookies from initial page load
            headers={
                "content-type": "application/json",
            },
            body=json.dumps({
                "applyDefaultCriteria":True,
                "employerId":employer_id,
                "employmentStatuses":["REGULAR","PART_TIME"],
                "jobTitle":None,
                "goc":None,
                "location":{},
                "defaultLanguage":"eng",
                "language":"eng",
                "mlHighlightSearch":None,
                "onlyCurrentEmployees":False,
                "overallRating":None,
                "pageSize":5,"page":page_number,
                "preferredTldId":0,
                "reviewCategories":[],
                "sort":"DATE",
                "textSearch":"",
                "worldwideFilter":False,
                "dynamicProfileId":dynamic_profile_id,
                "useRowProfileTldForRatings":True,
                "enableKeywordSearch":True
            })
        )

    def save_reviews(reviews: List[Dict], filepath: str):
        """Save reviews to JSON file"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(reviews, f, indent=2, ensure_ascii=False)
        log.info("saved {} reviews to {}", len(reviews), filepath)

    def load_existing_reviews(filepath: str) -> List[Dict]:
        """Load existing reviews from JSON file if it exists"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    review_data = []

    # Load existing reviews if resuming and output file exists
    if output_file and start_page > 1:
        review_data = load_existing_reviews(output_file)
        log.info("loaded {} existing reviews from {}", len(review_data), output_file)

    log.info("scraping reviews api requirements from {}", url)

    first_page_html = await SCRAPFLY.async_scrape(ScrapeConfig(url=url, **BASE_CONFIG))
    if isinstance(first_page_html, ScrapflyScrapeError):
        log.error(f"Failed to scrape the first page {url}, got: {first_page_html.message}")
        return review_data if review_data else []

    try:
        # Get debug directory from output_file path, or use current directory
        debug_dir = None
        if debug:
            if output_file:
                from pathlib import Path
                debug_dir = str(Path(output_file).parent)
            else:
                debug_dir = "."
        employer_metadata = parse_reviews_api_metadata(first_page_html, debug_dir=debug_dir)
        employer_id = employer_metadata['employer_id']
        dynamic_profile_id = employer_metadata['dynamic_profile_id']
    except (ValueError, KeyError) as e:
        log.error(f"Failed to parse employer metadata from {url}: {e}")
        return review_data if review_data else []

    # Get first page to determine total pages (or start_page if resuming)
    first_api_page = await SCRAPFLY.async_scrape(
        generate_api_request_config(employer_id, dynamic_profile_id, start_page)
    )

    if isinstance(first_api_page, ScrapflyScrapeError):
        log.error(f"Failed to scrape page {start_page}, got: {first_api_page.message}")
        return review_data if review_data else []

    first_page_data = json.loads(first_api_page.content)
    review_data.extend(first_page_data['data']['employerReviews']['reviews'])
    total_pages = first_page_data['data']['employerReviews']['numberOfPages']

    if max_pages:
        # Adjust max_pages relative to start_page
        end_page = min(start_page + max_pages - 1, total_pages)
    else:
        end_page = total_pages

    # Save after first page if output file specified
    if output_file:
        save_reviews(review_data, output_file)

    remaining_page_count = end_page - start_page
    if remaining_page_count <= 0:
        log.info("scraped {} reviews from {} (page {})", len(review_data), url, start_page)
        return review_data

    log.info("scraping reviews pagination from {}, scraping pages {} to {} ({} pages)",
             url, start_page + 1, end_page, remaining_page_count)

    # Process pages one at a time to enable incremental saves and better error handling
    for page_num in range(start_page + 1, end_page + 1):
        try:
            result = await SCRAPFLY.async_scrape(
                generate_api_request_config(employer_id, dynamic_profile_id, page_num)
            )

            if isinstance(result, ScrapflyScrapeError):
                log.error(f"Failed to scrape page {page_num}, got: {result.message}")
                log.info("Stopping at page {}. Resume with start_page={}", page_num - 1, page_num)
                break

            page_data = json.loads(result.content)
            new_reviews = page_data['data']['employerReviews']['reviews']
            review_data.extend(new_reviews)

            # Incremental save after each page
            if output_file:
                save_reviews(review_data, output_file)

            log.info("scraped page {}/{}, total reviews: {}", page_num, end_page, len(review_data))

        except Exception as e:
            log.error(f"Error on page {page_num}: {e}")
            log.info("Stopping at page {}. Resume with start_page={}", page_num - 1, page_num)
            break

    log.info("scraped {} reviews from {} (pages {} to {})", len(review_data), url, start_page, end_page)
    return review_data


def parse_salaries(result: ScrapeApiResponse) -> Dict:
    """Parse Glassdoor salaries page for salary data"""
    
    salary_data = {
        "results": [],
        "numPages": 1,
        "salaryCount": 0,
        "jobTitleCount": 0
    }
        
    salary_items = result.selector.css('[data-test="salary-item"]')
    
    for item in salary_items:
        job_title = item.css('.SalaryItem_jobTitle__XWGpT::text').get()
        if not job_title:
            continue
            
        salary_range = item.css('.SalaryItem_salaryRange__UL9vQ::text').get()
        salary_count_text = item.css('.SalaryItem_salaryCount__GT665::text').get() or ""
        
        salary_count = 0
        if "Salaries submitted" in salary_count_text:
            try:
                salary_count = int(salary_count_text.split()[0])
            except (ValueError, IndexError):
                pass
        
        salary_item = {
            "jobTitle": {
                "text": job_title,
            },
            "salaryCount": salary_count,
            "basePayStatistics": {
                "percentiles": []
            }
        }
        
        # Parse salary range
        if salary_range:
            range_clean = salary_range.replace('$', '').replace('K', '000')
            if ' - ' in range_clean:
                try:
                    min_str, max_str = range_clean.split(' - ')
                    min_salary = float(min_str.replace(',', ''))
                    max_salary = float(max_str.replace(',', ''))
                    salary_item["basePayStatistics"]["percentiles"] = [
                        {"ident": "min", "value": min_salary},
                        {"ident": "max", "value": max_salary}
                    ]
                except ValueError:
                    pass
        
        salary_data["results"].append(salary_item)
    
    # Extract pagination from HTML
    page_links = result.selector.css('.pagination_PageNumberText__F7427::text').getall()
    if page_links:
        try:
            salary_data["numPages"] = max(int(page) for page in page_links if page.isdigit())
        except ValueError:
            pass
    
    # Extract job title count from HTML
    result_count_text = result.selector.css('.SortBar_SearchCount__cYwt6::text').get() or ""
    if "job titles" in result_count_text:
        try:
            count_str = result_count_text.split()[0]
            salary_data["jobTitleCount"] = int(count_str.replace(',', ''))
        except (ValueError, IndexError):
            pass
    
    salary_data["salaryCount"] = len(salary_data["results"])
    
    log.info(f"Parsed {len(salary_data['results'])} salary items")
    return salary_data


async def scrape_salaries(url: str, max_pages: Optional[int] = None) -> Dict:
    """Scrape Glassdoor Salary page for salary listing data (with pagination)"""
    log.info("scraping salaries from {}", url)
    first_page = await SCRAPFLY.async_scrape(ScrapeConfig(url=url, **BASE_CONFIG))
    salaries = parse_salaries(first_page)
    total_pages = salaries["numPages"]
    if max_pages and total_pages > max_pages:
        total_pages = max_pages

    log.info("scraped first page of salaries of {}, scraping remaining {} pages", url, total_pages - 1)
    other_pages = [
        ScrapeConfig(url=Url.change_page(first_page.context["url"], page=page), **BASE_CONFIG)
        for page in range(2, total_pages + 1)
    ]
    async for result in SCRAPFLY.concurrent_scrape(other_pages):
        if not isinstance(result, ScrapflyScrapeError):
            salaries["results"].extend(parse_salaries(result)["results"])
        else:
            log.error(f"failed to scrape {result.api_response.config['url']}, got: {result.message}")
    log.info("scraped {} salaries from {} in {} pages", len(salaries["results"]), url, total_pages)
    return salaries


class FoundCompany(TypedDict):
    """type hint for company search result"""

    name: str
    id: int
    logoURL: str
    employerId: int
    employerName: str


async def find_companies(query: str) -> List[FoundCompany]:
    """find company Glassdoor ID and name by query. e.g. "ebay" will return "eBay" with ID 7853"""
    result = await SCRAPFLY.async_scrape(
        ScrapeConfig(
            url=f"https://www.glassdoor.com/api-web/employer/find.htm?autocomplete=true&maxEmployersForAutocomplete=50&term={query}",
            **BASE_CONFIG,
        )
    )
    data = json.loads(result.content)
    companies = []
    for result in data:
        companies.append(
            {
                "name": result["label"],
                "id": result["id"],
                "logoURL": result["logoURL"],
                "employerId": (
                    result["parentRelationshipVO"]["employerId"] if result["parentRelationshipVO"] is not None else None
                ),
                "employerName": (
                    result["parentRelationshipVO"]["employerName"]
                    if result["parentRelationshipVO"] is not None
                    else None
                ),
            }
        )
    return companies


class Region(Enum):
    """glassdoor.com region codes"""

    UNITED_STATES = "1"
    UNITED_KINGDOM = "2"
    CANADA_ENGLISH = "3"
    INDIA = "4"
    AUSTRALIA = "5"
    FRANCE = "6"
    GERMANY = "7"
    SPAIN = "8"
    BRAZIL = "9"
    NETHERLANDS = "10"
    AUSTRIA = "11"
    MEXICO = "12"
    ARGENTINA = "13"
    BELGIUM_NEDERLANDS = "14"
    BELGIUM_FRENCH = "15"
    SWITZERLAND_GERMAN = "16"
    SWITZERLAND_FRENCH = "17"
    IRELAND = "18"
    CANADA_FRENCH = "19"
    HONG_KONG = "20"
    NEW_ZEALAND = "21"
    SINGAPORE = "22"
    ITALY = "23"


class Url:
    """
    Helper URL generator that generates full URLs for glassdoor.com pages
    from given employer name and ID
    For example:
    > GlassdoorUrl.overview("eBay Motors Group", "4189745")
    https://www.glassdoor.com/Overview/Working-at-eBay-Motors-Group-EI_IE4189745.11,28.htm

    Note that URL formatting is important when it comes to scraping Glassdoor
    as unusual URL formats can lead to scraper blocking.
    """

    @staticmethod
    def overview(employer: str, employer_id: str, region: Optional[Region] = None) -> str:
        employer = employer.replace(" ", "-")
        url = f"https://www.glassdoor.com/Overview/Working-at-{employer}-EI_IE{employer_id}"
        # glassdoor is allowing any prefix for employer name and
        # to indicate the prefix suffix numbers are used like:
        # https://www.glassdoor.com/Overview/Working-at-eBay-Motors-Group-EI_IE4189745.11,28.htm
        # 11,28 is the slice where employer name is
        _start = url.split("/Overview/")[1].find(employer)
        _end = _start + len(employer)
        url += f".{_start},{_end}.htm"
        if region:
            return url + f"?filter.countryId={region.value}"
        return url

    @staticmethod
    def reviews(employer: str, employer_id: str, region: Optional[Region] = None) -> str:
        employer = employer.replace(" ", "-")
        url = f"https://www.glassdoor.com/Reviews/{employer}-Reviews-E{employer_id}.htm?"
        if region:
            return url + f"?filter.countryId={region.value}"
        return url

    @staticmethod
    def salaries(employer: str, employer_id: str, region: Optional[Region] = None) -> str:
        employer = employer.replace(" ", "-")
        url = f"https://www.glassdoor.com/Salary/{employer}-Salaries-E{employer_id}.htm?"
        if region:
            return url + f"?filter.countryId={region.value}"
        return url

    @staticmethod
    def jobs(employer: str, employer_id: str, region: Optional[Region] = None) -> str:
        employer = employer.replace(" ", "-")
        url = f"https://www.glassdoor.com/Jobs/{employer}-Jobs-E{employer_id}.htm?"
        if region:
            return url + f"?filter.countryId={region.value}"
        return url

    @staticmethod
    def change_page(url: str, page: int) -> str:
        """update page number in a glassdoor url"""
        if re.search(r"_P\d+\.htm", url):
            new = re.sub(r"(?:_P\d+)*.htm", f"_P{page}.htm", url)
        else:
            new = re.sub(".htm", f"_P{page}.htm", url)
        assert new != url
        return new
