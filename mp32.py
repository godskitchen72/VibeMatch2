import os
import re
import time
from flask import Flask, request, render_template, flash, redirect, url_for
from dotenv import load_dotenv
from supabase import create_client
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 1. Environment Parsing configuration
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.path.join(PROJECT_DIR, '.env')

if os.path.exists(ENV_FILE_PATH):
    load_dotenv(ENV_FILE_PATH)
    print("🏠 Running locally: Loaded configuration from .env file.")
else:
    print("☁️ Running in Cloud: Reading directly from Render Environment Variables.")

supabase_url = os.environ.get("SUPABASE_URL")
# Look for the master admin key instead of the public one
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") 

flask_secret = os.environ.get("FLASK_SECRET_KEY", "fallback-secret-key-for-dev")
youtube_api_key = os.environ.get("YOUTUBE_API_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("Stopping: SUPABASE_URL or SUPABASE_KEY is missing.")

app = Flask(__name__)
app.secret_key = flask_secret
supabase = create_client(supabase_url, supabase_key)

if not youtube_api_key:
    raise ValueError("YOUTUBE_API_KEY is missing from the environment configuration.")
youtube = build("youtube", "v3", developerKey=youtube_api_key)


def parse_iso8601_duration(duration_str):
    """
    Converts ISO 8601 duration string (e.g., PT1H23M45S or PT4M12S) 
    into a readable display format [HH:MM:SS] or [MM:SS] and returns total seconds.
    """
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(duration_str)
    if not match:
        return "[0:00]", 0

    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = int(match.group(3)) if match.group(3) else 0

    total_seconds = (hours * 3600) + (minutes * 60) + seconds

    if hours > 0:
        display = f"[{hours}:{minutes:02d}:{seconds:02d}]"
    else:
        display = f"[{minutes}:{seconds:02d}]"

    return display, total_seconds


def clean_and_filter_tracks(raw_items, video_details, filter_type):
    """
    Applies strict media filters, processes English/Russian language layers,
    and partitions outputs into Tracks (<=10m) or Sets (>10m).
    """
    filtered_tracks = []
    seen_urls = set()
    seen_normalized_titles = set()

    # Map video details by ID for fast O(1) dictionary lookups
    details_map = {v['id']: v['contentDetails']['duration'] for v in video_details}

    # Added 'фильм' and 'музыка' support to exclusion patterns for Russian data cleaning
    exclusion_patterns = re.compile(
        r'\b(remix|live|cover|karaoke|tribute|instrumental|reverb|slowed|sped up|mashup|bootleg|animation|edit|film|comédie|клип|караоке|кавер|лайв)\b', 
        re.IGNORECASE
    )
    normalization_pattern = re.compile(r'(\[official.*?\]|\(official.*?\)|hd|hq|official video|audio|original|премьера|релиз|\b20\d{2}\b)', re.IGNORECASE)
    clean_title_pattern = re.compile(r'(\[Official.*?\]|\(Official.*?\)|HD|HQ|Official Video|Audio|ORIGINAL|Премьера|Официальный клип)', re.IGNORECASE)

    for item in raw_items:
        if item['id']['kind'] != 'youtube#video':
            continue

        title = item['snippet']['title']
        video_id = item['id']['videoId']
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        if video_url in seen_urls:
            continue
        if exclusion_patterns.search(title):
            continue

        # Look up duration metadata
        iso_duration = details_map.get(video_id, "PT0M0S")
        duration_display, total_seconds = parse_iso8601_duration(iso_duration)

        # Radio button partition filtration rule
        if filter_type == "tracks" and total_seconds > 600:  # More than 10 minutes
            continue
        if filter_type == "sets" and total_seconds <= 600:   # 10 minutes or less
            continue

        # Normalization layer (preserves Cyrillic and alphanumeric text structures)
        norm_title = title.lower().replace("&#39;", "'").replace("&quot;", "")
        norm_title = normalization_pattern.sub('', norm_title)
        norm_title = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ]', '', norm_title)

        if norm_title in seen_normalized_titles or not norm_title:
            continue

        clean_title = clean_title_pattern.sub('', title).strip()
        clean_title = clean_title.replace("&#39;", "'").replace("&quot;", '"')

        # Append duration display right into the track title formatting footprint
        final_display_title = f"{clean_title} {duration_display}"

        filtered_tracks.append({
            "title": final_display_title,
            "video_url": video_url
        })
        
        seen_urls.add(video_url)
        seen_normalized_titles.add(norm_title)

        if len(filtered_tracks) >= 20:
            break

    return filtered_tracks


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    current_query = ""
    filter_type = "tracks"  # Default fallback initial selection
    
    if request.method == "POST":
        current_query = request.form.get("query", "").strip()
        filter_type = request.form.get("filter_type", "tracks")
        
        if current_query:
            try:
                start_time = time.time()
                
                # Fetch base search items
                search_response = youtube.search().list(
                    q=current_query,
                    part="snippet",
                    maxResults=30,
                    type="video"
                ).execute()
                
                raw_items = search_response.get("items", [])
                video_ids = [item['id']['videoId'] for item in raw_items if item['id']['kind'] == 'youtube#video']
                
                # Secondary Batch Query pipeline processing durations
                video_details = []
                if video_ids:
                    details_response = youtube.videos().list(
                        id=",".join(video_ids),
                        part="contentDetails"
                    ).execute()
                    video_details = details_response.get("items", [])

                results = clean_and_filter_tracks(raw_items, video_details, filter_type)

                if results:
                    db_start = time.time()
                    search_insert = supabase.table("searches").insert({"query_text": current_query}).execute()
                    
                    if search_insert and getattr(search_insert, 'data', None) and len(search_insert.data) > 0:
                        search_id = search_insert.data[0]["id"]
                        tracks_to_insert = [
                            {"search_id": search_id, "title": t["title"], "video_url": t["video_url"]}
                            for t in results
                        ]
                        supabase.table("tracks").insert(tracks_to_insert).execute()
                    print(f"🚀 Total server pipeline duration: {time.time() - start_time:.2f}s")
                else:
                    flash("No original matches found matching that configuration.", "info")

            except HttpError as e:
                flash(f"YouTube API Error: {e.reason}", "error")
            except Exception as e:
                flash(f"Application Error handled safely: {str(e)}", "error")

    return render_template("index.html", results=results, query=current_query, filter_type=filter_type)


@app.route("/history")
def history():
    try:
        response = supabase.table("searches").select("*").order("id", desc=True).execute()
        history_records = response.data if response else []
        return render_template("index.html", history=history_records, results=[], query="", filter_type="tracks")
    except Exception as e:
        flash(f"Error accessing history: {str(e)}", "error")
        return redirect(url_for("index"))


@app.route("/history/<int:search_id>")
def view_history_results(search_id):
    try:
        search_res = supabase.table("searches").select("query_text").eq("id", search_id).single().execute()
        query_text = search_res.data["query_text"] if search_res.data else "Historical Search"
        tracks_res = supabase.table("tracks").select("title", "video_url").eq("search_id", search_id).execute()
        results = tracks_res.data if tracks_res else []
        return render_template("index.html", results=results, query=query_text, filter_type="tracks")
    except Exception as e:
        flash(f"Error retrieving historic run: {str(e)}", "error")
        return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_debug = False if os.environ.get("PORT") else True
    app.run(host="0.0.0.0", port=port, debug=is_debug)
