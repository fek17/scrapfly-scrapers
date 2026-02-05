"""
This example run script shows how to run the Glassdoor.com scraper defined in ./glassdoor.py
It scrapes job, review and salary (TODO OVERVIEW?) data and saves it to ./results/

To run this script set the env variable $SCRAPFLY_KEY with your scrapfly API key:
$ export $SCRAPFLY_KEY="your key from https://scrapfly.io/dashboard"
"""
import asyncio
import json
from pathlib import Path
import glassdoor

output = Path(__file__).parent / "results"
output.mkdir(exist_ok=True)


async def run():
    # enable scrapfly cache for basic use
    glassdoor.BASE_CONFIG["cache"] = False

    print("running Glassdoor scrape and saving results to ./results directory")

    url = "https://www.glassdoor.com/Jobs/eBay-Jobs-E7853.htm?filter.countryId=1"
    # or use URL builder to build urls from company name and ID
    url = glassdoor.Url.jobs("eBay", "7853", region=glassdoor.Region.UNITED_STATES)
    result_jobs = await glassdoor.scrape_jobs(url, max_pages=3)
    output.joinpath("jobs.json").write_text(json.dumps(result_jobs, indent=2, ensure_ascii=False))

    url = "https://www.glassdoor.com/Salary/eBay-Salaries-E7853.htm"
    result_salaries = await glassdoor.scrape_salaries(url, max_pages=3)
    output.joinpath("salaries.json").write_text(json.dumps(result_salaries, indent=2, ensure_ascii=False))

    url = "https://www.glassdoor.com/Reviews/eBay-Reviews-E7853.htm"
    # Reviews scraper now supports incremental saves and resume capability:
    # - output_file: saves after each page (prevents data loss on failures)
    # - start_page: resume from a specific page if previous scrape was interrupted
    # - debug: saves HTML to results/debug_page.html when parsing fails
    reviews_file = str(output.joinpath("reviews.json"))
    result_reviews = await glassdoor.scrape_reviews(
        url,
        max_pages=3,
        output_file=reviews_file,  # enables incremental saves
        debug=True  # saves debug HTML on failure
    )
    # Note: when using output_file, data is saved automatically after each page
    # If not using output_file, save manually:
    # output.joinpath("reviews.json").write_text(json.dumps(result_reviews, indent=2, ensure_ascii=False))


async def run_reviews_with_resume():
    """
    Example: Resume a previously interrupted reviews scrape.

    If your scrape fails at page 50, you can resume from page 50:
    - The scraper will load existing reviews from the output file
    - Then continue scraping from start_page onwards
    - Each page is saved incrementally to prevent data loss
    """
    glassdoor.BASE_CONFIG["cache"] = False

    url = "https://www.glassdoor.com/Reviews/eBay-Reviews-E7853.htm"
    reviews_file = str(output.joinpath("reviews.json"))

    # Resume from page 50 (if previous scrape stopped there)
    result_reviews = await glassdoor.scrape_reviews(
        url,
        start_page=50,  # resume from page 50
        max_pages=100,  # scrape up to 100 more pages
        output_file=reviews_file  # incremental saves + loads existing data
    )
    print(f"Total reviews scraped: {len(result_reviews)}")


if __name__ == "__main__":
    asyncio.run(run())
    # To resume an interrupted scrape, use:
    # asyncio.run(run_reviews_with_resume())
