# =============================================================================
# Soluify.com  |  Your #1 IT Problem Solver  |  {SeerrBridge v0.3 Refactor}
# =============================================================================
#  __         _
# (_  _ |   .(_
# __)(_)||_||| \/
#              /
# © 2024
# -----------------------------------------------------------------------------


import requests
from RTN import RTN, Torrent, DefaultRanking
from RTN.models import SettingsModel, CustomRank
from RTN.exceptions import GarbageTorrent
from RTN.parser import title_match
from fastapi import FastAPI, Request
from pydantic import BaseModel, field_validator, ValidationError
from typing import Optional, List, Any, Dict, Literal
from loguru import logger
from bs4 import BeautifulSoup
import re
import os
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv

# Initialize FastAPI app
app = FastAPI()

# Constants for APIs
REAL_DEBRID_API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
TORRENTIO_API_URL = "https://torrentio.strem.fun/qualityfilter=scr,cam/stream/movie/{imdb_id}.json"
RD_INSTANT_AVAILABILITY_URL = f"{REAL_DEBRID_API_BASE_URL}/torrents/instantAvailability/{{hash}}"
RD_ADD_TORRENT_URL = f"{REAL_DEBRID_API_BASE_URL}/torrents/addMagnet"

# Rate limits for Real-Debrid API
MAX_CALLS_PER_MINUTE = 60

# Load environment variables from .env file
load_dotenv()

# Load Real-Debrid API key from environment variables
RD_API_KEY = os.getenv('RD_API_KEY')

# Pydantic Models for the Payload
MediaType = Literal["movie", "tv"]

class Media(BaseModel):
    media_type: MediaType
    status: str
    imdbId: Optional[str] = None
    tmdbId: int
    tvdbId: Optional[int] = None

    @field_validator("imdbId", mode="after")
    @classmethod
    def stringify_imdb_id(cls, value: Any) -> Optional[str]:
        if value and isinstance(value, int):
            return f"tt{int(value):07d}"
        return value

    @field_validator("tvdbId", "tmdbId", mode="before")
    @classmethod
    def validate_ids(cls, value: Any) -> Optional[int]:
        if value in ("", None):
            return None  # Convert empty string to None
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

class OverseerrWebhook(BaseModel):
    notification_type: str
    event: str
    subject: str
    message: Optional[str] = None
    image: Optional[str] = None
    media: Media
    extra: List[Dict[str, Any]] = []

# Step 1: Scrape movie details (title and release year) from TMDb
def scrape_movie_details_from_tmdb(tmdb_id: int) -> Optional[dict]:
    """
    Scrape movie title and release year from the TMDb movie page using the tmdbId.
    """
    url = f"https://www.themoviedb.org/movie/{tmdb_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Extract movie title
        title_element = soup.find('div', class_='title').find('a')
        movie_title = title_element.text.strip() if title_element else None
        
        # Extract release year
        release_date_element = soup.find('span', class_='tag release_date')
        release_year = release_date_element.text.strip("()") if release_date_element else None
        
        if movie_title and release_year:
            return {
                "title": movie_title,
                "release_year": release_year
            }
        else:
            logger.error("Failed to scrape movie title or release year.")
            return None
    else:
        logger.error(f"Failed to fetch TMDb page. Status code: {response.status_code}")
        return None

# Step 2: Search Google for the IMDb ID
def search_google_for_imdb_id(query: str) -> Optional[str]:
    """
    Search Google for the given query and extract the IMDb ID from the search results.
    """
    search_url = f"https://www.google.com/search?q={query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }
    
    response = requests.get(search_url, headers=headers)
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the IMDb ID by looking for the href in <a> tags that contain 'imdb.com/title'
        for a_tag in soup.find_all('a', href=True):
            if 'imdb.com/title' in a_tag['href']:
                imdb_link = a_tag['href']
                # Extract the IMDb ID from the link (e.g., tt9697780)
                imdb_id_match = re.search(r'tt\d+', imdb_link)
                if imdb_id_match:
                    return imdb_id_match.group(0)  # Return the IMDb ID (e.g., tt9697780)
        logger.error("IMDb ID not found in Google search results.")
        return None
    else:
        logger.error(f"Google search failed with status code {response.status_code}")
        return None

# Define the RTN settings model
settings = SettingsModel(
    require=["1080p", "4K"],  # Example requirements
    exclude=["CAM", "TS"],  # Example exclusions
    preferred=["HDR", "BluRay"],  # Example preferences
    custom_ranks={
        "uhd": CustomRank(enable=True, fetch=True, rank=200),
        "hdr": CustomRank(enable=True, fetch=True, rank=100),
        "fhd": CustomRank(enable=True, fetch=True, rank=90),
        "webdl": CustomRank(enable=True, fetch=True, rank=80),
        # Add more custom rankings if needed
    }
)

# Initialize RTN with settings and default ranking model
rtn = RTN(settings=settings, ranking_model=DefaultRanking())

# Step 3: Query Torrentio API to get available torrents
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def query_torrentio(imdb_id: str) -> Optional[Dict[str, Any]]:
    url = TORRENTIO_API_URL.format(imdb_id=imdb_id)
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Torrentio API failed with status code {response.status_code}")
        return None

# Step 4: Check torrent availability on Real-Debrid
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def check_rd_availability(info_hash: str) -> Optional[Dict[str, Any]]:
    url = RD_INSTANT_AVAILABILITY_URL.format(hash=info_hash)
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    response = requests.get(url, headers=headers)
    
    # Log the full response for debugging
    logger.debug(f"Real-Debrid response for info hash {info_hash}: {response.text}")
    
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 401:
        logger.error("Real-Debrid API key is invalid or expired.")
    else:
        logger.error(f"Real-Debrid instant availability failed with status code {response.status_code}")
    return None

def rank_and_check_torrents(torrentio_results, correct_title):
    """
    Rank torrents using RTN and check if any of them match the correct title.
    """
    torrents = set()
    for stream in torrentio_results['streams']:
        info_hash = stream.get('infoHash')
        raw_title = stream.get('title')

        if not info_hash or not raw_title:
            continue

        # Check if the title matches the correct title using RTN's title_match function
        if not title_match(correct_title, raw_title):
            continue  # Skip torrents that don't match the title

        try:
            # Rank the torrent using RTN
            torrent: Torrent = rtn.rank(raw_title, info_hash)
        except GarbageTorrent:
            logger.info(f"Skipping garbage torrent: {raw_title}")
            continue

        if torrent and torrent.fetch:
            # If the torrent is considered valid, add it to the set
            torrents.add(torrent)

    # Sort the list of torrents based on their rank in descending order
    sorted_torrents = sorted(list(torrents), key=lambda x: x.rank, reverse=True)
    return sorted_torrents

# Step 5: Add torrent to Real-Debrid and select specific files
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def add_torrent_and_select_files(info_hash: str, torrent_name: str, file_ids: str) -> Optional[Dict[str, Any]]:
    """
    Add a torrent to Real-Debrid and select specific files.
    
    :param info_hash: The info hash of the torrent.
    :param torrent_name: The name of the torrent.
    :param file_ids: Comma-separated file IDs to select (e.g., "1").
    :return: The response from the Real-Debrid API if successful, None otherwise.
    """
    url = RD_ADD_TORRENT_URL
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    data = {
        'magnet': f"magnet:?xt=urn:btih:{info_hash}&dn={torrent_name}"
    }
    response = requests.post(url, headers=headers, data=data)
    
    if response.status_code == 201:
        rd_response = response.json()
        torrent_id = rd_response.get('id')
        
        if torrent_id:
            logger.info(f"Torrent added to Real-Debrid successfully: {info_hash}")
            
            # Step 6: Select specific files in the torrent
            if select_files_in_rd(torrent_id, file_ids):
                return {"success": True, "message": f"Torrent added and files {file_ids} selected.", "torrent_id": torrent_id}
            else:
                return {"success": False, "message": "Failed to select files in the torrent."}
        else:
            logger.error("Torrent ID not found in Real-Debrid response.")
            return {"success": False, "message": "Torrent added, but torrent ID not found."}
    else:
        logger.error(f"Failed to add torrent to Real-Debrid with status code {response.status_code}")
        return None

# Step 6: Select specific files from the torrent in Real-Debrid
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def select_files_in_rd(torrent_id: str, file_ids: str) -> bool:
    """
    Select specific files in a Real-Debrid torrent using the torrent ID.
    
    :param torrent_id: The ID of the torrent in Real-Debrid.
    :param file_ids: A comma-separated string of file IDs to be selected (e.g., "1,2,3").
    :return: True if the files were successfully selected, False otherwise.
    """
    url = f"{REAL_DEBRID_API_BASE_URL}/torrents/selectFiles/{torrent_id}"
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    data = {
        'files': file_ids  # Comma-separated file IDs or "all"
    }
    
    response = requests.post(url, headers=headers, data=data)
    
    if response.status_code == 204:
        logger.info(f"Files {file_ids} successfully selected for torrent ID: {torrent_id}")
        return True
    else:
        logger.error(f"Failed to select files for torrent ID: {torrent_id}. Status code: {response.status_code}")
        return False

# FastAPI endpoint to receive the webhook payload from Jellyseer
@app.post("/jellyseer-webhook/")
async def jellyseer_webhook(request: Request) -> Dict[str, Any]:
    try:
        response = await request.json()

        # Check for test notification
        if response.get("subject") == "Test Notification":
            logger.info("Received test notification, Overseerr configured properly")
            return {"success": True, "message": "Test notification received successfully"}

        # Validate the incoming payload using the Pydantic model
        req = OverseerrWebhook.model_validate(response)

    except (Exception, ValidationError) as e:
        logger.error(f"Failed to process request: {e}")
        return {"success": False, "message": str(e)}

    try:
        # Step 1: Extract the tmdbId from the payload's media object
        tmdb_id = req.media.tmdbId
        logger.info(f"Received tmdbId: {tmdb_id} from Jellyseerr webhook.")

        # Step 2: Scrape the movie title and release year from TMDb
        logger.info(f"Scraping TMDb for movie details using tmdbId: {tmdb_id}...")
        movie_details = scrape_movie_details_from_tmdb(tmdb_id)
        if not movie_details:
            return {"success": False, "message": "Failed to scrape movie details from TMDb"}

        movie_title = movie_details['title']
        release_year = movie_details['release_year']
        correct_title = f"{movie_title} {release_year}"

        # Step 3: Query Torrentio API to get torrents
        logger.info(f"Querying Torrentio API for torrents with IMDb ID: {imdb_id}...")
        torrentio_results = query_torrentio(imdb_id)
        if not torrentio_results or not torrentio_results.get('streams'):
            logger.error("No torrents found on Torrentio.")
            return {"success": False, "message": "No torrents found"}

        # Step 6: Rank torrents and check for matches
        sorted_torrents = rank_and_check_torrents(torrentio_results, correct_title)
        if not sorted_torrents:
            logger.error("No matching torrents found after ranking.")
            return {"success": False, "message": "No matching torrents found"}

        # Step 7: Add the highest-ranked torrent to Real-Debrid
        for torrent in sorted_torrents:
            info_hash = torrent.infohash
            rd_availability = check_rd_availability(info_hash)
            if rd_availability and rd_availability.get(info_hash):
                # Add torrent to Real-Debrid and select file #1
                result = add_torrent_and_select_files(info_hash, movie_title, "1")
                return result

        logger.error("No torrents available on Real-Debrid.")
        return {"success": False, "message": "No torrents available on Real-Debrid"}

    except Exception as e:
        logger.error(f"Error processing webhook payload: {e}")
        return {"success": False, "message": str(e)}

# Main entry point for running the FastAPI server
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
