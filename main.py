# main.py - Flask Backend for Whapineo

import os
import json
import psycopg2
import requests # Now using requests for web fetching
from bs4 import BeautifulSoup # Now using BeautifulSoup for HTML parsing
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
from flask_cors import CORS
from urllib.parse import urlparse
import re # For regular expressions to parse follower count

# Load environment variables from .env file (for local development)
load_dotenv()

app = Flask(__name__)
# Initialize CORS - allows requests from all origins for now.
CORS(app)

# Database connection details
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Establishes and returns a PostgreSQL database connection."""
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL environment variable not set. Please configure it in Render.com environment variables.")
        raise ValueError("DATABASE_URL environment variable not set.")
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"ERROR: Failed to connect to the database using DATABASE_URL: {DATABASE_URL[:30]}... (truncated). Full error: {e}")
        raise # Re-raise the exception after printing

def initialize_db():
    """Initializes the database schema if tables do not exist."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Create channels table with follower_count
        # Ensure 'description' column exists, which it already does.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT, -- Description column already exists
                link VARCHAR(255) NOT NULL UNIQUE,
                average_rating REAL DEFAULT 0.0,
                ratings_count INTEGER DEFAULT 0,
                follower_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Create comments table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                author VARCHAR(255) DEFAULT 'Użytkownik',
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        print("Database tables checked/created successfully.")
    except Exception as e:
        print(f"ERROR: Error initializing database schema: {e}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Call initialize_db directly when the app starts.
try:
    with app.app_context():
        initialize_db()
except Exception as e:
    print(f"CRITICAL ERROR during database initialization: {e}")
    pass


def fetch_channel_details_from_whatsapp_page(url):
    """
    Attempts to scrape channel name, description, and follower count from the WhatsApp channel URL.
    WARNING: This approach is highly unreliable for dynamic websites like WhatsApp.
    WhatsApp uses JavaScript to render content and has strong anti-scraping measures.
    This function is for demonstration/attempt purposes.
    It is likely to be blocked or return incomplete data.
    """
    channel_name = None
    channel_description = None # New: for description
    follower_count = 0

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.75 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # --- Attempt to find Channel Name ---
        name_tag = soup.find('meta', property='og:title')
        if name_tag and name_tag.get('content'):
            channel_name = name_tag['content'].strip()
        else:
            # Fallback for name - often in <title> or <h1>
            name_tag = soup.find('h1', class_='_aj1s') # Specific class from inspecting WhatsApp web
            if name_tag:
                 channel_name = name_tag.get_text().strip()
            elif soup.find('title'):
                 channel_name = soup.find('title').get_text().replace(' | WhatsApp Channel', '').strip()
            if not channel_name: # Fallback to generic "WhatsApp Channel" if nothing found
                channel_name = "WhatsApp Channel"


        # --- Attempt to find Channel Description ---
        # Descriptions are highly variable. Try Open Graph first, then common HTML tags.
        description_tag = soup.find('meta', property='og:description')
        if description_tag and description_tag.get('content'):
            channel_description = description_tag['content'].strip()
        else:
            # Look for common paragraph or div tags near the name
            # This is even more speculative and likely to fail
            desc_tag = soup.find('span', {'dir': 'ltr', 'class': '_al_r'}) # Example based on some WhatsApp web elements
            if desc_tag:
                channel_description = desc_tag.get_text().strip()
            elif soup.find('p', class_='_aj1s'): # Another potential common text element
                 channel_description = soup.find('p', class_='_aj1s').get_text().strip()

        if not channel_description:
            channel_description = "Brak dostępnego opisu." # Default description if not found


        # --- Attempt to find Follower Count ---
        follower_element = soup.find(text=lambda text: text and ("obserwujący" in text.lower() or "followers" in text.lower()))
        if follower_element:
            try:
                text_with_followers = follower_element.strip()
                # Use regex to find digits (e.g., "150 obserwujących", "1.2K followers")
                match = re.search(r'(\d+(?:[.,]\d+)?)([KMBT]?)?\s*(obserwuj|follow)', text_with_followers, re.IGNORECASE)
                if match:
                    num_str = match.group(1).replace(',', '.') # Handle comma for decimal
                    num = float(num_str)
                    multiplier = 1
                    unit = (match.group(2) or '').upper()
                    if unit == 'K':
                        multiplier = 1000
                    elif unit == 'M':
                        multiplier = 1000000
                    elif unit == 'B':
                        multiplier = 1000000000
                    elif unit == 'T': # Not typical for followers, but for completeness
                        multiplier = 1000000000000
                    follower_count = int(num * multiplier)
            except ValueError:
                print(f"DEBUG: Could not parse follower count from: '{text_with_followers}'")
                pass

    except requests.exceptions.RequestException as req_err:
        print(f"ERROR: HTTP/Request error while fetching {url}: {req_err}")
        # Fallback names/descriptions if request fails
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallback to ID
        channel_description = "Nie udało się pobrać opisu z powodu błędu połączenia."
        follower_count = 0
    except Exception as e:
        print(f"ERROR: General error during scraping {url}: {e}")
        # Fallback names/descriptions if scraping fails
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallback to ID
        channel_description = "Nie udało się pobrać opisu z powodu błędu parsowania."
        follower_count = 0

    print(f"DEBUG: Scraped results for {url}: Name='{channel_name}', Description='{channel_description}', Followers={follower_count}")
    return {"name": channel_name, "description": channel_description, "follower_count": follower_count}


@app.route('/')
def index():
    """Serves the main HTML page or acts as a health check."""
    return "Backend dla Whapineo działa. Dostęp do /api/channels, etc."

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Fetches all channels from the database."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Select the new follower_count column
        cur.execute("SELECT id, name, description, link, average_rating, ratings_count, follower_count FROM channels ORDER BY created_at DESC;")
        channels_data = cur.fetchall()

        channels_list = []
        for channel in channels_data:
            channel_id = channel[0]
            # Fetch comments for each channel
            cur.execute("SELECT author, text, created_at FROM comments WHERE channel_id = %s ORDER BY created_at DESC;", (channel_id,))
            comments_data = cur.fetchall()
            comments_list = [{
                'author': c[0],
                'text': c[1],
                'date': c[2].isoformat().split('T')[0]
            } for c in comments_data]

            channels_list.append({
                'id': channel_id,
                'name': channel[1],
                'description': channel[2],
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'followerCount': channel[6] if channel[6] is not None else 0,
                'comments': comments_list
            })
        return jsonify(channels_list)
    except Exception as e:
        print(f"ERROR: Błąd podczas pobierania kanałów: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels', methods=['POST'])
def add_channel():
    """Dodaje nowy kanał do bazy danych, próbując pobrać nazwę, opis i liczbę obserwujących z linku."""
    data = request.get_json()
    link = data.get('link') # Only link is expected from frontend now

    if not link:
        return jsonify({"error": "Pole 'link' jest wymagane."}), 400

    # Validate WhatsApp link
    parsed_url = urlparse(link)
    if not (parsed_url.netloc.endswith('whatsapp.com') and parsed_url.path.startswith('/channel/')):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp. Oczekiwany format: https://whatsapp.com/channel/..."}), 400

    # Attempt to scrape details from the actual channel page
    scraped_data = fetch_channel_details_from_whatsapp_page(link)
    name = scraped_data['name']
    description = scraped_data['description']
    follower_count = scraped_data['follower_count']


    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO channels (name, description, link, follower_count) VALUES (%s, %s, %s, %s) RETURNING id;",
            (name, description, link, follower_count) # Now inserting description and follower_count
        )
        channel_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"message": "Kanał dodany pomyślnie", "id": channel_id, "name": name, "description": description, "followerCount": follower_count}), 201
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        print(f"ERROR: Naruszenie unikalności podczas dodawania kanału z linkiem: {link}")
        return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
    except Exception as e:
        print(f"ERROR: Błąd podczas dodawania kanału: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania kanału"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/rate', methods=['POST'])
def rate_channel(channel_id):
    """Przesyła ocenę dla kanału i aktualizuje jego średnią ocenę."""
    data = request.get_json()
    rating = data.get('rating')

    if rating is None or not (1 <= rating <= 5):
        return jsonify({"error": "Ocena musi być liczbą od 1 do 5."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT average_rating, ratings_count FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()

        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        current_avg_rating = result[0] if result[0] is not None else 0.0
        current_ratings_count = result[1] if result[1] is not None else 0

        new_total_rating = (current_avg_rating * current_ratings_count) + rating
        new_ratings_count = current_ratings_count + 1
        new_avg_rating = new_total_rating / new_ratings_count

        cur.execute(
            "UPDATE channels SET average_rating = %s, ratings_count = %s WHERE id = %s;",
            (new_avg_rating, new_ratings_count, channel_id)
        )
        conn.commit()
        return jsonify({"message": "Ocena dodana pomyślnie", "new_average_rating": new_avg_rating, "new_ratings_count": new_ratings_count}), 200
    except Exception as e:
        print(f"ERROR: Błąd podczas oceniania kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania oceny"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/comments', methods=['POST'])
def add_comment_to_channel(channel_id):
    """Dodaje komentarz do określonego kanału."""
    data = request.get_json()
    text = data.get('text')
    author = data.get('author', 'Anonimowy')

    if not text:
        return jsonify({"error": "Tekst komentarza jest wymagany."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO comments (channel_id, author, text) VALUES (%s, %s, %s);",
            (channel_id, author, text)
        )
        conn.commit()
        return jsonify({"message": "Komentarz dodany pomyślnie"}), 201
    except Exception as e:
        print(f"ERROR: Błąd podczas dodawania komentarza do kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania komentarza"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
