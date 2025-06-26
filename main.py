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
    """Establishes and returns a database connection."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def is_valid_whatsapp_channel_link(link):
    """Validates if a link is a valid WhatsApp channel link."""
    # Regex for a typical WhatsApp channel link format
    regex = r"^https?:\/\/(?:www\.)?whatsapp\.com\/channel\/.+"
    return re.match(regex, link) is not None

def get_channel_metadata(link):
    """
    Scrapes metadata (name, description, profile image) from a WhatsApp channel link.
    Returns a dictionary with the metadata.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(link, headers=headers, timeout=10)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract data from meta tags
        name = soup.find('meta', {'property': 'og:title'})['content'] if soup.find('meta', {'property': 'og:title'}) else 'Brak nazwy'
        description = soup.find('meta', {'property': 'og:description'})['content'] if soup.find('meta', {'property': 'og:description'}) else 'Brak opisu'
        profile_image_url = soup.find('meta', {'property': 'og:image'})['content'] if soup.find('meta', {'property': 'og:image'}) else None
        
        # Extract follower count from the page content
        follower_span = soup.find('span', class_=lambda x: x and 'x193iq5w' in x and 'x10pd1g4' in x)
        follower_count_str = follower_span.text if follower_span else '0'
        # Parse the follower count, handling different language formats and 'K' for thousands
        follower_count = 0
        if 'K' in follower_count_str:
            follower_count = int(float(follower_count_str.replace('K', '').replace(',', '.')) * 1000)
        elif 'M' in follower_count_str:
            follower_count = int(float(follower_count_str.replace('M', '').replace(',', '.')) * 1_000_000)
        else:
            follower_count = int(re.search(r'\d+', follower_count_str).group()) if re.search(r'\d+', follower_count_str) else 0

        # The description might contain the follower count, so clean it up if needed
        # Not strictly necessary if we get it from a dedicated span, but good for robustness
        clean_description = description.split('·')[0].strip() if '·' in description else description

        return {
            'name': name,
            'description': clean_description,
            'profile_image_url': profile_image_url,
            'follower_count': follower_count
        }
    except Exception as e:
        print(f"ERROR: Błąd podczas pobierania metadanych dla linku {link}: {e}")
        return None

def create_tables():
    """Creates database tables if they do not exist."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                link VARCHAR(255) UNIQUE NOT NULL,
                description TEXT,
                profile_image_url VARCHAR(255),
                rating FLOAT DEFAULT 0.0,
                ratings_count INTEGER DEFAULT 0,
                follower_count INTEGER DEFAULT 0,
                last_boosted TIMESTAMP WITH TIME ZONE DEFAULT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
                author VARCHAR(100) NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        print("INFO: Tabele bazy danych zostały pomyślnie utworzone lub już istniały.")
    except Exception as e:
        print(f"ERROR: Błąd podczas tworzenia tabel: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Call the function to create tables on application startup
create_tables()

# --- API Endpoints ---

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """
    Retrieves all channels from the database, sorted by last_boosted timestamp.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Primary sort is by last_boosted (descending), with NULLs at the end.
        # This ensures boosted channels are at the top.
        cur.execute(
            "SELECT id, name, link, description, profile_image_url, rating, ratings_count, follower_count, last_boosted FROM channels ORDER BY last_boosted DESC NULLS LAST, id DESC;"
        )
        channels_data = cur.fetchall()

        channels_list = []
        for row in channels_data:
            channel_id = row[0]
            # Fetch comments for each channel
            cur.execute(
                "SELECT author, text, created_at FROM comments WHERE channel_id = %s ORDER BY created_at DESC;",
                (channel_id,)
            )
            comments_data = cur.fetchall()
            comments_list = [
                {'author': c[0], 'text': c[1], 'date': c[2].strftime('%Y-%m-%d %H:%M')}
                for c in comments_data
            ]
            
            channels_list.append({
                'id': row[0],
                'name': row[1],
                'link': row[2],
                'description': row[3],
                'profile_image_url': row[4],
                'rating': row[5],
                'ratings_count': row[6],
                'follower_count': row[7],
                'last_boosted': row[8].isoformat() if row[8] else None,
                'comments': comments_list
            })

        return jsonify(channels_list)
    except Exception as e:
        print(f"ERROR: Błąd podczas pobierania kanałów: {e}")
        return jsonify({"error": "Błąd podczas pobierania kanałów z bazy danych"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels', methods=['POST'])
def add_channel():
    """
    Adds a new channel to the database. Scrapes metadata from the link.
    """
    data = request.get_json()
    link = data.get('link')

    if not link:
        return jsonify({"error": "Link jest wymagany."}), 400

    if not is_valid_whatsapp_channel_link(link):
        return jsonify({"error": "Nieprawidłowy format linku do kanału WhatsApp."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if the channel already exists in the database
        cur.execute("SELECT id FROM channels WHERE link = %s;", (link,))
        if cur.fetchone():
            return jsonify({"error": "Ten kanał już istnieje w bazie danych."}), 409 # Conflict

        # Scrape metadata from the link
        metadata = get_channel_metadata(link)
        if not metadata:
            return jsonify({"error": "Nie udało się pobrać metadanych z linku. Upewnij się, że link jest poprawny."}), 422 # Unprocessable Entity

        cur.execute(
            """
            INSERT INTO channels (name, link, description, profile_image_url, follower_count)
            VALUES (%s, %s, %s, %s, %s) RETURNING id;
            """,
            (metadata['name'], link, metadata['description'], metadata['profile_image_url'], metadata['follower_count'])
        )
        new_channel_id = cur.fetchone()[0]
        conn.commit()

        return jsonify({"message": "Kanał dodany pomyślnie", "id": new_channel_id}), 201
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        return jsonify({"error": "Kanał o podanym linku już istnieje w bazie danych."}), 409
    except Exception as e:
        print(f"ERROR: Błąd podczas dodawania kanału: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": f"Wystąpił nieoczekiwany błąd: {e}"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/<int:channel_id>', methods=['DELETE'])
def delete_channel(channel_id):
    """
    Deletes a channel by ID. Requires admin token.
    """
    if not verify_admin_token(request.headers):
        abort(403) # Forbidden
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM channels WHERE id = %s RETURNING id;", (channel_id,))
        deleted_id = cur.fetchone()
        conn.commit()
        if deleted_id:
            return jsonify({"message": f"Kanał o ID {channel_id} został usunięty."}), 200
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
def delete_all_channels():
    """
    Deletes all channels and their comments. Requires admin token.
    """
    if not verify_admin_token(request.headers):
        abort(403) # Forbidden
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Deleting from channels will cascade and delete comments as well
        cur.execute("DELETE FROM channels;")
        conn.commit()
        return jsonify({"message": "Wszystkie kanały i komentarze zostały usunięte."}), 200
    except Exception as e:
        print(f"ERROR: Błąd podczas usuwania wszystkich kanałów: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas czyszczenia bazy danych."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/rate', methods=['POST'])
def rate_channel(channel_id):
    """
    Updates the rating for a given channel based on a new rating.
    """
    data = request.get_json()
    rating = data.get('rating')

    if rating is None or not 1 <= rating <= 5:
        return jsonify({"error": "Ocena musi być wartością od 1 do 5."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Fetch current rating and count
        cur.execute("SELECT rating, ratings_count FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()
        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
        
        current_rating = result[0] or 0.0
        ratings_count = result[1] or 0
        
        # Calculate new rating (simple average)
        new_ratings_count = ratings_count + 1
        new_rating = ((current_rating * ratings_count) + rating) / new_ratings_count

        # Update the database
        cur.execute(
            "UPDATE channels SET rating = %s, ratings_count = %s WHERE id = %s;",
            (new_rating, new_ratings_count, channel_id)
        )
        conn.commit()
        
        return jsonify({"message": "Ocena dodana pomyślnie", "new_rating": new_rating, "new_ratings_count": new_ratings_count}), 200
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
        return jsonify({"error": "Błąd podczas dodawania komentarza."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/boost', methods=['POST'])
def boost_channel(channel_id):
    """
    Updates the last_boosted timestamp for a given channel to the current time.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE channels SET last_boosted = NOW() WHERE id = %s;",
            (channel_id,)
        )
        
        if cur.rowcount == 0:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        conn.commit()
        return jsonify({"message": "Kanał został zboostowany!"}), 200
    except Exception as e:
        print(f"ERROR: Błąd podczas boostowania kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas boostowania kanału."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/unboost', methods=['POST'])
def unboost_channel(channel_id):
    """
    Sets the last_boosted timestamp to a very old date to push the channel to the bottom.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Update the last_boosted timestamp to a very old date (Unix Epoch)
        # This will push it to the end of the DESC NULLS LAST sort.
        unboost_timestamp = datetime(1970, 1, 1)
        cur.execute(
            "UPDATE channels SET last_boosted = %s WHERE id = %s;",
            (unboost_timestamp, channel_id)
        )
        
        if cur.rowcount == 0:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        conn.commit()
        return jsonify({"message": "Kanał został 'unboostowany'."}), 200
    except Exception as e:
        print(f"ERROR: Błąd podczas unboostowania kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas unboostowania kanału."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# --- Admin Panel Endpoints ---

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """
    Authenticates an admin user with a password.
    """
    data = request.get_json()
    password = data.get('password')
    
    if password == ADMIN_PASS1 or password == ADMIN_PASS2:
        return jsonify({"token": ADMIN_AUTH_TOKEN, "message": "Logowanie powiodło się!"}), 200
    else:
        return jsonify({"error": "Nieprawidłowe hasło."}), 401 # Unauthorized

if __name__ == '__main__':
    # When running locally, use a default host/port.
    # On Render, the host and port are set by the environment.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
