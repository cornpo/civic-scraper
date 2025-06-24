import logging
from datetime import datetime
from urllib.parse import urlparse, urljoin
import re

from civic_scraper import base
from civic_scraper.base.asset import Asset, AssetCollection
from civic_scraper.base.cache import Cache # Assuming Cache might be used later
import civic_scraper # For version

logger = logging.getLogger(__name__)

class CivicClerkApiSite(base.Site):
    """
    Scraper for modern CivicClerk sites that use an OData API.
    Example API base: https://stockbridgega.api.civicclerk.com/v1/
    Example events endpoint: https://stockbridgega.api.civicclerk.com/v1/Events
    """
    def __init__(self, url, place=None, state_or_province=None, cache=Cache(), **kwargs):
        # Pass only known arguments to base.Site.__init__
        super().__init__(url, place=place, state_or_province=state_or_province, cache=cache)

        # Consume any kwargs relevant to this class, if any, here.
        # For now, we are not using any specific kwargs in this subclass beyond what base.Site uses.

        # The input URL might be the portal URL (e.g., https://stockbridgega.portal.civicclerk.com)
        # or potentially the API URL itself. We need to derive the API base.
        parsed_url = urlparse(url)
        if "api.civicclerk.com" in parsed_url.netloc:
            self.api_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}/v1/"
            # Attempt to derive portal subdomain for instance name if API url is given
            # e.g. stockbridgega from stockbridgega.api.civicclerk.com
            subdomain_match = re.match(r"([^.]+)\.api\.civicclerk\.com", parsed_url.netloc)
            if subdomain_match:
                self.civicclerk_instance = subdomain_match.group(1)
            else:
                # Fallback, though less ideal
                self.civicclerk_instance = parsed_url.netloc.split('.')[0]
        else:
            # Assuming portal URL like https://stockbridgega.portal.civicclerk.com
            # Derive API base: https://stockbridgega.api.civicclerk.com/v1/
            subdomain = parsed_url.netloc.split('.')[0]
            self.civicclerk_instance = subdomain
            self.api_base_url = f"{parsed_url.scheme}://{subdomain}.api.civicclerk.com/v1/"

        # Ensure session is available, from base class or initialize if not
        if not hasattr(self, 'session') or self.session is None:
            from requests import Session
            self.session = Session()
            self.session.headers["User-Agent"] = (
                f"civic-scraper/{civic_scraper.__version__}"
            )
            # Optional: Add hook for error status codes, if not handled by base class
            # self.session.hooks = {
            #     "response": lambda r, *args, **kwargs: r.raise_for_status()
            # }


    def scrape(self, start_date_str, end_date_str, **kwargs):
        logger.info(f"Scraping {self.url} (API: {self.api_base_url}) from {start_date_str} to {end_date_str}")
        asset_collection = AssetCollection()

        # Convert start_date and end_date to ISO 8601 format for API
        # Assuming start_date is beginning of day, end_date is end of day
        start_datetime = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date_str + "T23:59:59.999", "%Y-%m-%dT%H:%M:%S.%f")

        start_date_iso = start_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date_iso = end_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")

        page_size = 50
        odata_filter = f"startDateTime ge {start_date_iso} and startDateTime le {end_date_iso}"
        orderby = "startDateTime asc" # Process oldest first within the range

        current_api_url = (
            f"{self.api_base_url}Events?"
            f"$filter={odata_filter}&"
            f"$orderby={orderby}&"
            f"$top={page_size}"
        )

        request_count = 0
        max_requests = 20 # Safety break for pagination

        while current_api_url and request_count < max_requests:
            request_count += 1
            try:
                response = self.session.get(current_api_url, timeout=30)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                data = response.json()
            except Exception as e:
                logger.error(f"API request to {current_api_url} failed: {e}")
                break # Stop if there's an error

            events = data.get('value', [])
            if not events:
                logger.info("No events found in the current API response page.")
                break

            for event in events:
                event_id = event.get('id')
                event_name = event.get('eventName')
                start_dt_str = event.get('startDateTime')

                if not all([event_id, event_name, start_dt_str]):
                    logger.warning(f"Skipping event with missing critical data: {event}")
                    continue

                # Location details
                location_info = event.get('eventLocation', {})
                full_address_parts = [
                    location_info.get('address1'),
                    location_info.get('address2'),
                    location_info.get('city'),
                    location_info.get('state'),
                    location_info.get('zipCode')
                ]
                full_address = ", ".join(filter(None, full_address_parts))
                if not full_address:
                    full_address = "Location not specified"


                published_files = event.get('publishedFiles', [])
                if not published_files:
                    # Create an asset for the meeting itself if no files are present
                    # This ensures the meeting is recorded even if it has no downloadable documents
                    meeting_datetime_obj = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                    asset_args = {
                        "url": None, # No specific file URL
                        "asset_name": event_name,
                        "committee_name": event.get('eventCategoryName', event_name),
                        "place": self.place or full_address, # Use specific place if available
                        "state_or_province": self.state_or_province or location_info.get('state'),
                        "asset_type": "Meeting", # Generic type for the event itself
                        "meeting_date": meeting_datetime_obj.date(),
                        "meeting_time": meeting_datetime_obj.time(),
                        "meeting_id": f"civicclerkapi_{self.civicclerk_instance}_{event_id}",
                        "scraped_by": f"civic-scraper_{civic_scraper.__version__}",
                        "content_type": None,
                        "content_length": None,
                    }
                    asset_collection.append(Asset(**asset_args))

                for file_data in published_files:
                    file_id = file_data.get('fileId')
                    file_name = file_data.get('name')
                    file_type = file_data.get('type')
                    relative_file_url = file_data.get('url')

                    if not all([file_id, file_name, file_type, relative_file_url]):
                        logger.warning(f"Skipping file with missing data for event {event_name}: {file_data}")
                        continue

                    asset = self._create_asset_from_api_data(event, file_data, full_address)
                    asset_collection.append(asset)

            current_api_url = data.get('@odata.nextLink')
            if current_api_url:
                # Ensure the nextLink is absolute or make it absolute
                if not current_api_url.startswith(('http://', 'https://')):
                    current_api_url = urljoin(self.api_base_url, current_api_url.lstrip('/'))
                logger.info(f"Fetching next page: {current_api_url}")
            else:
                logger.info("No more pages to fetch.")
                break

        if request_count >= max_requests:
            logger.warning(f"Reached maximum request limit ({max_requests}) for pagination. Results may be incomplete.")

        return asset_collection

    def _create_asset_from_api_data(self, event_data, file_data, full_address):
        meeting_datetime = datetime.fromisoformat(event_data['startDateTime'].replace('Z', '+00:00'))

        # The file_data['url'] is like "stream/STOCKBRIDGEGA/0e424e2f-bcef-408f-b01f-484ee0deab85.pdf"
        # This seems to be relative to the *API base URL* for this specific API structure.
        # Example: https://stockbridgega.api.civicclerk.com/v1/stream/STOCKBRIDGEGA/...
        # Let's ensure it's joined correctly with the API base, not the portal URL.
        download_url = urljoin(self.api_base_url, file_data['url'].lstrip('/'))

        # Alternative: Use GetMeetingFileStream if the above direct URL doesn't work or isn't reliable
        # download_url = f"{self.api_base_url}Meetings/GetMeetingFileStream(fileId={file_data['fileId']},plainText=false)"

        asset_args = {
            "url": download_url,
            "asset_name": file_data['name'],
            "committee_name": event_data.get('eventCategoryName', event_data.get('eventName')),
            "place": self.place or full_address, # Use specific place if available from eventLocation
            "state_or_province": self.state_or_province or event_data.get('eventLocation', {}).get('state'),
            "asset_type": file_data['type'],
            "meeting_date": meeting_datetime.date(),
            "meeting_time": meeting_datetime.time(),
            "meeting_id": f"civicclerkapi_{self.civicclerk_instance}_{event_data['id']}",
            "scraped_by": f"civic-scraper_{civic_scraper.__version__}",
            "content_type": None,
            "content_length": None,
        }
        return Asset(**asset_args)
