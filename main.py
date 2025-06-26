# main.py - Flask Backend for Whapineo (without user accounts/JWT)

import os
import json
import psycopg2
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, abort
from datetime import datetime
from dotenv import load_dotenv
from flask_cors import CORS
from urllib.parse import urlparse
import re

# Load environment variables from .env file (for local development)
load_dotenv()

app = Flask(__name__)
# Initialize CORS - allows requests from all origins (*)
CORS(app)

# Database connection details
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- Admin Panel Passwords (from Environment Variables) ---
# This remains separate for admin panel authentication.
ADMIN_PASS1 = os.environ.get('ADMIN_PASS1', 'default_admin123') # Ustaw to na Render.com
ADMIN_PASS2 = os.environ.get('ADMIN_PASS2', 'default_superadmin456') # Ustaw to na Render.com
ADMIN_AUTH_TOKEN = os.environ.get('ADMIN_AUTH_TOKEN', 'very_secret_admin_token') # Token do autoryzacji operacji

def verify_admin_token(request_headers):
    """Verifies if the request contains the correct admin authentication token."""
    auth_header = request_headers.get('Authorization')
    if not auth_header:
        return False
    try:
        # Expected format: "Bearer <token>"
        token_type, token = auth_header.split(None, 1)
    except ValueError:
        return False
    if token_type.lower() == 'bearer' and token == ADMIN_AUTH_TOKEN:
        return True
    return False


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
        raise

def initialize_db():
    """Initializes the database schema if tables do not exist."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Usunięto tabelę users
        # Zmieniono Channels, aby nie odwoływały się do user_id
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                link VARCHAR(500) NOT NULL UNIQUE,
                profile_image_url VARCHAR(500),
                follower_count INTEGER DEFAULT 0,
                average_rating REAL DEFAULT 0.0,
                ratings_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Zmieniono Comments, aby nie odwoływały się do author_id
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                author VARCHAR(255) DEFAULT 'Anonimowy użytkownik', -- Autor jako zwykły string
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Zmieniono Ratings, aby nie odwoływały się do user_id
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                score INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE
                -- Usunięto user_id
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
    channel_description = None
    follower_count = 0
    profile_image_url = None

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
            name_tag = soup.find('h1', class_='_aj1s') # Example based on some WhatsApp web elements
            if name_tag:
                 channel_name = name_tag.get_text().strip()
            elif soup.find('title'):
                 channel_name = soup.find('title').get_text().replace(' | WhatsApp Channel', '').strip()
            if not channel_name:
                channel_name = "WhatsApp Channel" # Fallback if no name found

        # --- Attempt to find Channel Description ---
        description_tag = soup.find('meta', property='og:description')
        if description_tag and description_tag.get('content'):
            channel_description = description_tag['content'].strip()
        else:
            desc_tag = soup.find('span', {'dir': 'ltr', 'class': '_al_r'}) # Common description element
            if desc_tag:
                channel_description = desc_tag.get_text().strip()
            elif soup.find('p', class_='_aj1s'): # Another possible description element
                 channel_description = soup.find('p', class_='_aj1s').get_text().strip()

        if not channel_description:
            channel_description = "Brak dostępnego opisu." # Default description

        # --- Attempt to find Follower Count ---
        # This is highly unreliable due to dynamic content and anti-scraping.
        follower_element = soup.find(text=lambda text: text and ("obserwujący" in text.lower() or "followers" in text.lower()))
        if follower_element:
            try:
                text_with_followers = follower_element.strip()
                match = re.search(r'(\d+(?:[.,]\d+)?)([KMBT]?)?\s*(obserwuj|follow)', text_with_followers, re.IGNORECASE)
                if match:
                    num_str = match.group(1).replace(',', '.')
                    num = float(num_str)
                    multiplier = 1
                    unit = (match.group(2) or '').upper()
                    if unit == 'K':
                        multiplier = 1000
                    elif unit == 'M':
                        multiplier = 1000000
                    elif unit == 'B':
                        multiplier = 1000000000
                    elif unit == 'T':
                        multiplier = 1000000000000
                    follower_count = int(num * multiplier)
            except ValueError:
                print(f"DEBUG: Could not parse follower count from: '{text_with_followers}'")
                pass # Keep default 0

        # --- Attempt to find Profile Image URL ---
        image_tag = soup.find('meta', property='og:image')
        if image_tag and image_tag.get('content'):
            profile_image_url = image_tag['content'].strip()
        else:
            # Look for common image elements within the page (e.g., img tags with specific classes)
            img_element = soup.find('img', class_='_aj3u') # Example based on some WhatsApp web elements
            if img_element and img_element.get('src'):
                profile_image_url = img_element['src'].strip()


    except requests.exceptions.RequestException as req_err:
        print(f"ERROR: HTTP/Request error while fetching {url}: {req_err}")
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallback name
        channel_description = "Nie udało się pobrać opisu z powodu błędu połączenia."
        follower_count = 0
        profile_image_url = None
    except Exception as e:
        print(f"ERROR: General error during scraping {url}: {e}")
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallback name
        channel_description = "Nie udało się pobrać opisu z powodu błędu parsowania."
        follower_count = 0
        profile_image_url = None

    print(f"DEBUG: Scraped results for {url}: Name='{channel_name}', Description='{channel_description}', Followers={follower_count}, Image='{profile_image_url}'")
    return {"name": channel_name, "description": channel_description, "follower_count": follower_count, "profile_image_url": profile_image_url}


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
        cur.execute("SELECT id, name, description, link, average_rating, ratings_count, follower_count, profile_image_url FROM channels ORDER BY created_at DESC;")
        channels_data = cur.fetchall()

        channels_list = []
        for channel in channels_data:
            channel_id = channel[0]
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
                'profileImageUrl': channel[7], # Dodano profile_image_url
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
    link = data.get('link')
    # Frontend może wysłać name/description/profileImageUrl, ale backend to nadpisze scrapingiem.
    # Używamy ich jako fallback, jeśli scraping zawiedzie.
    frontend_name = data.get('name')
    frontend_description = data.get('description')
    frontend_profile_image_url = data.get('profileImageUrl')


    if not link:
        return jsonify({"error": "Pole 'link' jest wymagane."}), 400

    parsed_url = urlparse(link)
    if not (parsed_url.netloc.endswith('whatsapp.com') and parsed_url.path.startswith('/channel/')):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp. Oczekiwany format: https://whatsapp.com/channel/..."}), 400

    # Próba scrapingu
    scraped_data = fetch_channel_details_from_whatsapp_page(link)
    name = scraped_data['name'] if scraped_data['name'] else frontend_name # Użyj scraped, fallback do frontend
    description = scraped_data['description'] if scraped_data['description'] else frontend_description # Użyj scraped, fallback do frontend
    follower_count = scraped_data['follower_count']
    profile_image_url = scraped_data['profile_image_url'] if scraped_data['profile_image_url'] else frontend_profile_image_url

    # Ostateczne fallbacki, jeśli scraping i frontend nie dostarczyły danych
    if not name:
        name = parsed_url.path.strip('/').split('/')[-1] # Fallback to ID if scraping/frontend name fails
    if not description:
        description = "Brak dostępnego opisu."
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO channels (name, description, link, follower_count, profile_image_url) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
            (name, description, link, follower_count, profile_image_url)
        )
        channel_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({
            "message": "Kanał dodany pomyślnie",
            "id": channel_id,
            "name": name,
            "description": description,
            "followerCount": follower_count,
            "profileImageUrl": profile_image_url
        }), 201
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

# --- Admin API Endpoints (pozostają zabezpieczone tokenem) ---
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Authenticates admin users and returns a token."""
    data = request.get_json()
    pass1 = data.get('passwordOne')
    pass2 = data.get('passwordTwo')

    if not ADMIN_PASS1 or not ADMIN_PASS2:
        return jsonify({'error': 'Hasła administratora nie są skonfigurowane na serwerze.'}), 500

    if pass1 == ADMIN_PASS1 and pass2 == ADMIN_PASS2:
        return jsonify({"message": "Login successful", "token": ADMIN_AUTH_TOKEN}), 200
    else:
        return jsonify({"error": "Nieprawidłowe hasła administratora."}), 401

@app.route('/api/channels/<int:channel_id>', methods=['DELETE'])
def delete_channel(channel_id):
    """Deletes a specific channel from the database (admin only)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Unauthorized: Invalid or missing token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM channels WHERE id = %s RETURNING id;", (channel_id,))
        deleted_id = cur.fetchone()
        conn.commit()

        if deleted_id:
            return jsonify({"message": f"Kanał o ID {channel_id} został pomyślnie usunięty."}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
    except Exception as e:
        print(f"ERROR: Błąd podczas usuwania kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas usuwania kanału."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/all', methods=['DELETE'])
def clear_all_channels():
    """Deletes all channels and comments from the database (admin only)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Unauthorized: Invalid or missing token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Order matters: comments first due to foreign key constraint, then ratings
        cur.execute("DELETE FROM comments;")
        cur.execute("DELETE FROM ratings;") # Usunięto tabelę comments, więc trzeba usunąć ratings
        cur.execute("DELETE FROM channels;")
        conn.commit()
        return jsonify({"message": "Wszystkie kanały i komentarze zostały usunięte."}), 200
    except Exception as e:
        print(f"ERROR: Błąd podczas czyszczenia wszystkich kanałów: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas czyszczenia wszystkich kanałów."}), 500
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

        # Get current rating and count
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
    author = data.get('author', 'Anonimowy użytkownik') # Default author if not provided by frontend

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
