import os
import re
import time
from flask import Flask, request, render_template, flash, redirect, url_for
from dotenv import load_dotenv
from supabase import create_client
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 1. Look for a local .env file path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.path.join(PROJECT_DIR, '.env')

# 2. ONLY attempt to load it if the file actually exists (Local Phone Mode)
if os.path.exists(ENV_FILE_PATH):
    load_dotenv(ENV_FILE_PATH)
    print("🏠 Running locally: Loaded configuration from .env file.")
else:
    # If it doesn't exist, Python will seamlessly read from Render's Environment tab automatically
    print("☁️ Running in Cloud: Reading directly from Render Environment Variables.")

# 3. Fetch the config variables (works flawlessly for both environments now)
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
flask_secret = os.environ.get("FLASK_SECRET_KEY", "fallback-secret-key-for-dev")
youtube_api_key = os.environ.get("YOUTUBE_API_KEY")

# 4. Diagnostic check modified for cloud environments
if not supabase_url or not supabase_key:
    print("\n❌ CONFIGURATION ERROR DETECTED")
    print("Could not find SUPABASE_URL or SUPABASE_KEY in the system environment.")
    raise ValueError("Stopping: SUPABASE_URL or SUPABASE_KEY is missing.")

# 5. Initialize Flask
app = Flask(__name__)
app.secret_key = flask_secret

# 6. Initialize Supabase Client
supabase = create_client(supabase_url, supabase_key)

# 7. Initialize YouTube Client
if not youtube_api_key:
    raise ValueError("YOUTUBE_API_KEY is missing from the environment configuration.")
youtube = build("youtube", "v3", developerKey=youtube_api_key)

print("✅ Success! Flask, Supabase, and YouTube initialized perfectly.")


def clean_and_filter_tracks(raw_items):
    """
    Applies media architect filtering rules: removes duplicates, live versions, 
    remixes, covers, karaoke, and non-official media styles.
    """
    filtered_tracks = []
    seen_urls = set()
    seen_normalized_titles = set()

    # Pre-compiled regex patterns for optimal iteration performance
    exclusion_patterns = re.compile(
        r'\b(remix|live|cover|karaoke|tribute|instrumental|reverb|slowed|sped up|mashup|bootleg|animation|edit|film|comédie)\b', 
        re.IGNORECASE
    )
    normalization_pattern_1 = re.compile(r'(\[official.*?\]|\(official.*?\)|hd|hq|official video|audio|original|\b20\d{2}\b)', re.IGNORECASE)
    normalization_pattern_2 = re.compile(r'(\bhotel costes \d*?\b|\bhotel costes\b|a decade cd\d)', re.IGNORECASE)
    clean_title_pattern = re.compile(r'(\[Official.*?\]|\(Official.*?\)|HD|HQ|Official Video|Audio|ORIGINAL)', re.IGNORECASE)

    for item in raw_items:
        if item['id']['kind'] != 'youtube#video':
            continue

        title = item['snippet']['title']
        video_id = item['id']['videoId']
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        # Rule 1: Structural URL De-duplication
        if video_url in seen_urls:
            continue

        # Rule 2: Omit non-originals
        if exclusion_patterns.search(title):
            continue

        # Rule 3: Check if extended mixes are unauthorized additions
        if "extended" in title.lower() and not ("official" in title.lower() or "original" in title.lower()):
            continue

        # --- OPTIMIZED TITLE NORMALIZATION ---
        norm_title = title.lower().replace("&#39;", "'").replace("&quot;", "")
        norm_title = normalization_pattern_1.sub('', norm_title)
        norm_title = normalization_pattern_2.sub('', norm_title)
        norm_title = re.sub(r'[^a-z0-9]', '', norm_title)

        if norm_title in seen_normalized_titles or not norm_title:
            continue
        # ----------------------------------------------------

        # Clean title strings of typical YouTube clutter for UI display
        clean_title = clean_title_pattern.sub('', title).strip()
        clean_title = clean_title.replace("&#39;", "'").replace("&quot;", '"')

        filtered_tracks.append({
            "title": clean_title,
            "video_url": video_url
        })
        
        seen_urls.add(video_url)
        seen_normalized_titles.add(norm_title)

        if len(filtered_tracks) >= 20:  # Cap at 20 clean items for faster UI render performance
            break

    return filtered_tracks


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    current_query = ""
    
    if request.method == "POST":
        current_query = request.form.get("query", "").strip()
        
        if current_query:
            try:
                # --- OPTIMIZATION: TIME BENCHMARKING START ---
                start_time = time.time()
                
                print(f"Connecting to YouTube API for query: '{current_query}'...")
                # PERFORMANCE TWEAK: Lowered maxResults from 50 to 30 to significantly cut API download latency
                search_response = youtube.search().list(
                    q=current_query,
                    part="snippet",
                    maxResults=30,
                    type="video"
                ).execute()
                
                youtube_duration = time.time() - start_time
                print(f"⏱️ YouTube API responded in: {youtube_duration:.2f}s")

                filter_start = time.time()
                raw_items = search_response.get("items", [])
                results = clean_and_filter_tracks(raw_items)
                print(f"⏱️ Filtering logic took: {time.time() - filter_start:.2f}s")

                if results:
                    db_start = time.time()
                    print("Connecting to Supabase API...")
                    search_insert = supabase.table("searches").insert({"query_text": current_query}).execute()
                    
                    if search_insert and getattr(search_insert, 'data', None) and len(search_insert.data) > 0:
                        search_id = search_insert.data[0]["id"]
                        
                        tracks_to_insert = [
                            {"search_id": search_id, "title": t["title"], "video_url": t["video_url"]}
                            for t in results
                        ]
                        # PERFORMANCE TWEAK: Pushing pre-built collection blocks over in one seamless roundtrip execution
                        supabase.table("tracks").insert(tracks_to_insert).execute()
                    
                    print(f"⏱️ Supabase data logging finalized in: {time.time() - db_start:.2f}s")
                    print(f"🚀 Total server pipeline duration: {time.time() - start_time:.2f}s")
                else:
                    flash("No original matches found matching that description.", "info")

            except HttpError as e:
                print(f"YouTube API Error: {e}")
                flash(f"YouTube API Error: {e.reason}", "error")
            except OSError as e:
                print(f"Network OS Error: {e}")
                if e.errno == 103:
                    flash("Network aborted by device. Please ensure your phone's battery saver is off.", "error")
                else:
                    flash(f"Network Error: {str(e)}", "error")
            except Exception as e:
                print(f"❌ FATAL ERROR CAUGHT: {str(e)}")
                flash(f"Application Error handled safely: {str(e)}", "error")

    return render_template("index.html", results=results, query=current_query)


@app.route("/history")
def history():
    """Fetches all previous search operations ordered by date execution."""
    try:
        response = supabase.table("searches").select("*").order("id", desc=True).execute()
        history_records = response.data if response else []
        return render_template("index.html", history=history_records, results=[], query="")
    except Exception as e:
        flash(f"Error accessing history: {str(e)}", "error")
        return redirect(url_for("index"))


@app.route("/history/<int:search_id>")
def view_history_results(search_id):
    """Retrieves standard snapshot items tied to a historic lookup ID."""
    try:
        search_res = supabase.table("searches").select("query_text").eq("id", search_id).single().execute()
        query_text = search_res.data["query_text"] if search_res.data else "Historical Search"
        
        tracks_res = supabase.table("tracks").select("title", "video_url").eq("search_id", search_id).execute()
        results = tracks_res.data if tracks_res else []
        
        return render_template("index.html", results=results, query=query_text)
    except Exception as e:
        flash(f"Error retrieving historic run: {str(e)}", "error")
        return redirect(url_for("index"))


if __name__ == "__main__":
    # Dynamically read the assignment port given by Render's environment, fallback to 5000 locally
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
