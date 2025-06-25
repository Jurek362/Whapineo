# main.py - Flask Backend for Whapineo

import os
import json
import psycopg2
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
from flask_cors import CORS # Import CORS

# Load environment variables from .env file (for local development)
load_dotenv()

app = Flask(__name__)
# Initialize CORS - allows requests from all origins for now.
# For production, it's recommended to restrict this to specific origins (e.g., your frontend URL).
CORS(app) 

# Database connection details
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Establishes and returns a PostgreSQL database connection."""
    if not DATABASE_URL:
        # This error will now be printed explicitly before raising
        print("ERROR: DATABASE_URL environment variable not set. Please configure it in Render.com environment variables.")
        raise ValueError("DATABASE_URL environment variable not set.")
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        # Print the actual database connection error
        print(f"ERROR: Failed to connect to the database using DATABASE_URL: {DATABASE_URL[:30]}... (truncated). Full error: {e}")
        raise # Re-raise the exception after printing

def initialize_db():
    """Initializes the database schema if tables do not exist."""
    conn = None
    cur = None # Initialize cursor outside try block
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Create channels table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                link VARCHAR(255) NOT NULL UNIQUE,
                average_rating REAL DEFAULT 0.0,
                ratings_count INTEGER DEFAULT 0,
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
        raise # Re-raise to ensure Gunicorn fails with a clear message
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Call initialize_db directly when the app starts.
# This ensures tables are created/checked before any requests are handled.
# This part is crucial for Render's startup process.
try:
    with app.app_context(): # Ensure app context is available for database operations
        initialize_db()
except Exception as e:
    print(f"CRITICAL ERROR during database initialization: {e}")
    # Depending on how critical DB is, you might want to exit here
    # For now, let's allow the app to try and start, but log the error.
    # If the app fails to start after this, the Gunicorn traceback will eventually show it.
    pass # Allow the app to proceed even if DB init fails, to see other errors.
         # For production, you'd likely want this to stop the app if DB isn't ready.


@app.route('/')
def index():
    """Serves the main HTML page or acts as a health check."""
    # In a setup with a separate frontend, this might just be a simple health check.
    return "Backend for Whapineo is running. Access /api/channels, etc."

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Fetches all channels from the database."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name, description, link, average_rating, ratings_count FROM channels ORDER BY created_at DESC;")
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
                'date': c[2].isoformat().split('T')[0] # Format date as YYYY-MM-DD
            } for c in comments_data]

            channels_list.append({
                'id': channel_id,
                'name': channel[1],
                'description': channel[2],
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'comments': comments_list
            })
        return jsonify(channels_list)
    except Exception as e:
        print(f"ERROR: Error fetching channels: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels', methods=['POST'])
def add_channel():
    """Adds a new channel to the database."""
    data = request.get_json()
    name = data.get('name')
    description = data.get('description')
    link = data.get('link')

    if not name or not description or not link:
        return jsonify({"error": "Wszystkie pola są wymagane: nazwa, opis, link"}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO channels (name, description, link) VALUES (%s, %s, %s) RETURNING id;",
            (name, description, link)
        )
        channel_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"message": "Kanał dodany pomyślnie", "id": channel_id}), 201
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback() # Rollback the transaction
        print(f"ERROR: Unique violation when adding channel with link: {link}")
        return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
    except Exception as e:
        print(f"ERROR: Error adding channel: {e}")
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
    """Submits a rating for a channel and updates its average rating."""
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

        # Calculate new average rating
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
        print(f"ERROR: Error rating channel {channel_id}: {e}")
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
    """Adds a comment to a specific channel."""
    data = request.get_json()
    text = data.get('text')
    author = data.get('author', 'Anonimowy') # Default author if not provided

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
        print(f"ERROR: Error adding comment to channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania komentarza"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(debug=True, port=5000) # Run on port 5000 for local development
