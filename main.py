# main.py - Flask Backend for Whapineo (without user accounts/JWT)

import os
import json
import psycopg2
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, abort, send_from_directory
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask_cors import CORS
from urllib.parse import urlparse
import re
import logging
import csv
import io
from functools import wraps
import time
from collections import defaultdict

# Load environment variables from .env file (for local development)
load_dotenv()

app = Flask(__name__)
# Initialize CORS - allows requests from all origins (*)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database connection details
DATABASE_URL = os.environ.get('DATABASE_URL')

# Rate limiting storage (in production, use Redis or similar)
rate_limit_storage = defaultdict(list)

# --- Admin Panel Passwords (from Environment Variables) ---
# This remains separate for admin panel authentication.
ADMIN_PASS1 = os.environ.get('ADMIN_PASS1', 'default_admin123') # Ustaw to na Render.com
ADMIN_PASS2 = os.environ.get('ADMIN_PASS2', 'default_superadmin456') # Ustaw to na Render.com
ADMIN_AUTH_TOKEN = os.environ.get('ADMIN_AUTH_TOKEN', 'very_secret_admin_token') # Token do autoryzacji operacji

# Global flag to ensure database initialization runs only once
db_initialized = False

# Security and validation functions
def rate_limit(max_requests=60, per_seconds=3600):
    """Rate limiting decorator - max_requests per per_seconds"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_ip = request.remote_addr or 'unknown'
            current_time = time.time()
            
            # Clean old entries
            rate_limit_storage[client_ip] = [
                timestamp for timestamp in rate_limit_storage[client_ip]
                if current_time - timestamp < per_seconds
            ]
            
            # Check rate limit
            if len(rate_limit_storage[client_ip]) >= max_requests:
                logger.warning(f"Rate limit exceeded for IP: {client_ip}")
                return jsonify({"error": "Zbyt wiele żądań. Spróbuj ponownie później."}), 429
            
            # Add current request
            rate_limit_storage[client_ip].append(current_time)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def sanitize_text(text, max_length=1000):
    """Sanitize and validate text input"""
    if not text or not isinstance(text, str):
        return ""
    
    # Remove excessive whitespace and limit length
    text = text.strip()[:max_length]
    
    # Basic HTML/XSS prevention
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'[^\w\s\-_.,:;!?()\[\]\u00C0-\u017F\u0400-\u04FF]', '', text)
    
    return text

def validate_url(url):
    """Validate and sanitize URL"""
    if not url or not isinstance(url, str):
        return False
    
    try:
        result = urlparse(url.strip())
        return all([result.scheme in ['http', 'https'], result.netloc])
    except:
        return False

def validate_category(category):
    """Validate category input"""
    if not category or not isinstance(category, str):
        return "Ogólne"
    
    category = sanitize_text(category, 50)
    if not category:
        return "Ogólne"
    
    return category

def validate_rating(rating):
    """Validate rating input"""
    try:
        rating = int(rating)
        return 1 <= rating <= 5
    except (ValueError, TypeError):
        return False

def verify_admin_token(request_headers):
    """Weryfikuje, czy żądanie zawiera poprawny token autoryzacji administratora."""
    auth_header = request_headers.get('Authorization')
    if not auth_header:
        return False
    try:
        # Oczekiwany format: "Bearer <token>"
        token_type, token = auth_header.split(None, 1)
    except ValueError:
        return False
    if token_type.lower() == 'bearer' and token == ADMIN_AUTH_TOKEN:
        return True
    return False


def get_db_connection():
    """Ustanawia i zwraca połączenie z bazą danych PostgreSQL."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable not set")
        raise ValueError("Zmienna środowiskowa DATABASE_URL nie jest ustawiona.")
    try:
        # Upewnij się, że sslmode='require' jest używane dla Render.com
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        raise

def get_analytics_data():
    """Get analytics data for the admin dashboard"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        analytics = {}
        
        # Total channels
        cur.execute("SELECT COUNT(*) FROM channels;")
        analytics['total_channels'] = cur.fetchone()[0]
        
        # Partner channels
        cur.execute("SELECT COUNT(*) FROM channels WHERE is_partner = TRUE;")
        analytics['partner_channels'] = cur.fetchone()[0]
        
        # Total comments
        cur.execute("SELECT COUNT(*) FROM comments;")
        analytics['total_comments'] = cur.fetchone()[0]
        
        # Total ratings
        cur.execute("SELECT COUNT(*) FROM ratings;")
        analytics['total_ratings'] = cur.fetchone()[0]
        
        # Average rating across all channels
        cur.execute("SELECT AVG(rating) FROM channels WHERE rating > 0;")
        avg_rating = cur.fetchone()[0]
        analytics['average_rating'] = round(float(avg_rating), 2) if avg_rating else 0.0
        
        # Top rated channels (limit 5)
        cur.execute("""
            SELECT name, rating, ratings_count 
            FROM channels 
            WHERE rating > 0 AND ratings_count > 0
            ORDER BY rating DESC, ratings_count DESC 
            LIMIT 5;
        """)
        analytics['top_rated'] = [
            {'name': row[0], 'rating': float(row[1]), 'ratings_count': row[2]}
            for row in cur.fetchall()
        ]
        
        # Most commented channels (limit 5)
        cur.execute("""
            SELECT c.name, COUNT(co.id) as comment_count
            FROM channels c
            LEFT JOIN comments co ON c.id = co.channel_id
            GROUP BY c.id, c.name
            HAVING COUNT(co.id) > 0
            ORDER BY comment_count DESC
            LIMIT 5;
        """)
        analytics['most_commented'] = [
            {'name': row[0], 'comment_count': row[1]}
            for row in cur.fetchall()
        ]
        
        # Channels by category
        cur.execute("""
            SELECT category, COUNT(*) 
            FROM channels 
            GROUP BY category 
            ORDER BY COUNT(*) DESC;
        """)
        analytics['channels_by_category'] = [
            {'category': row[0] or 'Ogólne', 'count': row[1]}
            for row in cur.fetchall()
        ]
        
        # Recent activity (last 7 days)
        cur.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM channels 
            WHERE created_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC;
        """)
        analytics['recent_channels'] = [
            {'date': row[0].isoformat(), 'count': row[1]}
            for row in cur.fetchall()
        ]
        
        return analytics
        
    except Exception as e:
        logger.error(f"Error getting analytics data: {e}")
        return {}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def export_channels_data(format_type='csv'):
    """Export channels data in CSV or JSON format"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT c.id, c.name, c.description, c.link, c.rating, c.ratings_count, 
                   c.follower_count, c.category, c.is_partner, c.created_at,
                   COUNT(co.id) as comment_count
            FROM channels c
            LEFT JOIN comments co ON c.id = co.channel_id
            GROUP BY c.id, c.name, c.description, c.link, c.rating, c.ratings_count,
                     c.follower_count, c.category, c.is_partner, c.created_at
            ORDER BY c.created_at DESC;
        """)
        
        channels_data = []
        for row in cur.fetchall():
            channels_data.append({
                'id': row[0],
                'name': row[1],
                'description': row[2],
                'link': row[3],
                'rating': float(row[4]) if row[4] else 0.0,
                'ratings_count': row[5] or 0,
                'follower_count': row[6] or 0,
                'category': row[7] or 'Ogólne',
                'is_partner': row[8],
                'created_at': row[9].isoformat() if row[9] else '',
                'comment_count': row[10]
            })
        
        if format_type.lower() == 'json':
            return json.dumps(channels_data, indent=2, ensure_ascii=False)
        else:  # CSV
            output = io.StringIO()
            if channels_data:
                fieldnames = channels_data[0].keys()
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(channels_data)
            return output.getvalue()
            
    except Exception as e:
        logger.error(f"Error exporting data: {e}")
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def initialize_db():
    """Inicjalizuje schemat bazy danych, jeśli tabele nie istnieją,
    oraz aktualizuje istniejące kolumny."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Utwórz tabelę 'channels' z kolumną 'rating'
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                link VARCHAR(500) NOT NULL UNIQUE,
                profile_image_url VARCHAR(500),
                follower_count INTEGER DEFAULT 0,
                rating REAL DEFAULT 0.0,
                ratings_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_boosted TIMESTAMP DEFAULT NULL,
                is_partner BOOLEAN DEFAULT FALSE,
                category VARCHAR(255) DEFAULT 'Ogólne',
                view_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Add indexes for better performance
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_channels_rating ON channels(rating DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_channels_category ON channels(category);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_channels_partner ON channels(is_partner);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_channels_created_at ON channels(created_at);
        """)
        
        conn.commit()
        logger.info("Channels table created/updated successfully")

        # Spróbuj zmienić nazwę kolumny 'average_rating' na 'rating', jeśli istnieje
        try:
            cur.execute("""
                ALTER TABLE channels RENAME COLUMN average_rating TO rating;
            """)
            conn.commit()
            logger.info("Column 'average_rating' renamed to 'rating' successfully")
        except psycopg2.ProgrammingError as e:
            if conn and not conn.autocommit:
                 conn.rollback()
            if "column \"average_rating\" does not exist" in str(e):
                logger.info("Column 'average_rating' does not exist, no need to rename")
            elif "column \"rating\" already exists" in str(e):
                logger.info("Column 'rating' already exists")
            else:
                logger.warning(f"Unexpected error during column rename: {e}")
        except Exception as e:
            if conn and not conn.autocommit:
                conn.rollback()
            logger.warning(f"General error during ALTER TABLE: {e}")

        # Add new columns if they don't exist
        new_columns = [
            ("is_partner", "BOOLEAN DEFAULT FALSE"),
            ("category", "VARCHAR(255) DEFAULT 'Ogólne'"),
            ("view_count", "INTEGER DEFAULT 0"),
            ("last_updated", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        ]
        
        for column_name, column_def in new_columns:
            try:
                cur.execute(f"""
                    ALTER TABLE channels ADD COLUMN IF NOT EXISTS {column_name} {column_def};
                """)
                conn.commit()
                logger.info(f"Column '{column_name}' checked/added successfully")
            except Exception as e:
                if conn and not conn.autocommit:
                    conn.rollback()
                logger.warning(f"Error adding column '{column_name}': {e}")

        # Utwórz inne tabele (comments i ratings), jeśli nie istnieją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                author VARCHAR(255) DEFAULT 'Anonimowy użytkownik',
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_approved BOOLEAN DEFAULT TRUE
            );
        """)
        
        # Add indexes for comments
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_channel_id ON comments(channel_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_created_at ON comments(created_at);
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                score INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                ip_address INET DEFAULT NULL
            );
        """)
        
        # Add indexes for ratings
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ratings_channel_id ON ratings(channel_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ratings_timestamp ON ratings(timestamp);
        """)
        
        # Create admin logs table for audit trail
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(255) NOT NULL,
                target_type VARCHAR(100),
                target_id INTEGER,
                details TEXT,
                ip_address INET,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Add index for admin logs
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_logs_timestamp ON admin_logs(timestamp);
        """)
        
        conn.commit()
        logger.info("All database tables created/updated successfully")
        
    except Exception as e:
        logger.error(f"Error initializing database schema: {e}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Inicjalizacja bazy danych przy starcie aplikacji
# Używamy app.before_request wraz z flagą, aby upewnić się, że to zostanie wywołane raz
@app.before_request
def setup_database_once():
    global db_initialized
    if not db_initialized:
        try:
            initialize_db()
            db_initialized = True
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.critical(f"Failed to initialize database: {e}")
            pass

def log_admin_action(action, target_type=None, target_id=None, details=None):
    """Log admin actions for audit trail"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        ip_address = request.remote_addr or None
        
        cur.execute("""
            INSERT INTO admin_logs (action, target_type, target_id, details, ip_address)
            VALUES (%s, %s, %s, %s, %s);
        """, (action, target_type, target_id, details, ip_address))
        
        conn.commit()
        logger.info(f"Admin action logged: {action}")
        
    except Exception as e:
        logger.error(f"Error logging admin action: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def is_valid_whatsapp_channel_link(link):
    """
    Sprawdza, czy podany link jest prawidłowym linkiem do kanału WhatsApp,
    uwzględniając oczekiwany format ID kanału (min. 24 znaki alfanumeryczne).
    """
    # Regex for a typical WhatsApp channel link format with a stricter ID check.
    # [a-zA-Z0-9]{24,} ensures at least 24 alphanumeric characters for the ID.
    # (?:\/.*)? handles optional trailing slashes or other path segments.
    regex = r"^https?:\/\/(?:www\.)?whatsapp\.com\/channel\/[a-zA-Z0-9]{24,}(?:\/.*)?$"
    return re.match(regex, link) is not None


def fetch_channel_details_from_whatsapp_page(url):
    """
    Próbuje pobrać nazwę kanału, opis i liczbę obserwujących z adresu URL kanału WhatsApp.
    OSTRZEŻENIE: Ta metoda jest wysoce zawodna dla dynamicznych stron internetowych, takich jak WhatsApp.
    WhatsApp używa JavaScript do renderowania treści i ma silne środki antyskapingowe.
    Ta funkcja służy do celów demonstracyjnych/próbnych.
    Prawdopodobnie zostanie zablokowana lub zwróci niekompletne dane.
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

        # --- Próba znalezienia nazwy kanału ---
        name_tag = soup.find('meta', property='og:title')
        if name_tag and name_tag.get('content'):
            channel_name = name_tag['content'].strip()
        else:
            name_tag = soup.find('h1', class_='_aj1s') # Przykład na podstawie niektórych elementów WhatsApp web
            if name_tag:
                 channel_name = name_tag.get_text().strip()
            elif soup.find('title'):
                 channel_name = soup.find('title').get_text().replace(' | WhatsApp Channel', '').strip()
            if not channel_name:
                channel_name = "Kanał WhatsApp" # Fallback, jeśli nie znaleziono nazwy

        # --- Próba znalezienia opisu kanału ---
        description_tag = soup.find('meta', property='og:description')
        if description_tag and description_tag.get('content'):
            channel_description = description_tag['content'].strip()
        else:
            desc_tag = soup.find('span', {'dir': 'ltr', 'class': '_al_r'}) # Wspólny element opisu
            if desc_tag:
                channel_description = desc_tag.get_text().strip()
            elif soup.find('p', class_='_aj1s'): # Inny możliwy element opisu
                 channel_description = soup.find('p', class_='_aj1s').get_text().strip()

        if not channel_description:
            channel_description = "Brak dostępnego opisu." # Domyślny opis

        # --- Próba znalezienia liczby obserwujących ---
        # Jest to bardzo zawodne ze względu na dynamiczną zawartość i środki antyskapingowe.
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
                print(f"DEBUG: Nie można było sparsować liczby obserwujących z: '{text_with_followers}'")
                pass # Zachowaj domyślne 0

        # --- Próba znalezienia adresu URL obrazu profilowego ---
        image_tag = soup.find('meta', property='og:image')
        if image_tag and image_tag.get('content'):
            profile_image_url = image_tag['content'].strip()
        else:
            # Szukaj typowych elementów obrazów na stronie (np. tagi img z określonymi klasami)
            img_element = soup.find('img', class_='_aj3u') # Przykład na podstawie niektórych elementów WhatsApp web
            if img_element and img_element.get('src'):
                profile_image_url = img_element['src'].strip()


    except requests.exceptions.RequestException as req_err:
        print(f"ERROR: Błąd HTTP/żądania podczas pobierania {url}: {req_err}")
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallbackowa nazwa
        channel_description = "Nie udało się pobrać opisu z powodu błędu połączenia."
        follower_count = 0
        profile_image_url = None
    except Exception as e:
        print(f"ERROR: Ogólny błąd podczas skanowania {url}: {e}")
        parsed_url = urlparse(url)
        channel_name = parsed_url.path.strip('/').split('/')[-1] # Fallbackowa nazwa
        channel_description = "Nie udało się pobrać opisu z powodu błędu parsowania."
        follower_count = 0
        profile_image_url = None

    print(f"DEBUG: Wyniki skanowania dla {url}: Nazwa='{channel_name}', Opis='{channel_description}', Obserwujący={follower_count}, Obraz='{profile_image_url}'")
    return {"name": channel_name, "description": channel_description, "follower_count": follower_count, "profile_image_url": profile_image_url}


@app.route('/')
def index():
    """Serves the main HTML page"""
    try:
        return send_from_directory('.', 'index.html')
    except:
        return "Backend dla Whapineo działa. Dostęp do /api/channels, etc."

@app.route('/admin')
def admin():
    """Serves the admin HTML page"""
    try:
        return send_from_directory('.', 'admin.html')
    except:
        return "Panel administratora niedostępny."

# New Analytics and Export Endpoints
@app.route('/api/admin/analytics', methods=['GET'])
def get_analytics():
    """Get analytics data for admin dashboard"""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403
    
    try:
        analytics = get_analytics_data()
        log_admin_action("VIEW_ANALYTICS", details="Viewed analytics dashboard")
        return jsonify(analytics)
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        return jsonify({"error": "Błąd podczas pobierania danych analitycznych"}), 500

@app.route('/api/admin/export', methods=['GET'])
def export_data():
    """Export channels data in CSV or JSON format"""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403
    
    format_type = request.args.get('format', 'csv').lower()
    
    if format_type not in ['csv', 'json']:
        return jsonify({"error": "Nieprawidłowy format. Użyj 'csv' lub 'json'"}), 400
    
    try:
        data = export_channels_data(format_type)
        if data is None:
            return jsonify({"error": "Błąd podczas eksportu danych"}), 500
        
        log_admin_action("EXPORT_DATA", details=f"Exported data in {format_type.upper()} format")
        
        if format_type == 'json':
            return app.response_class(
                data,
                mimetype='application/json',
                headers={"Content-Disposition": f"attachment; filename=channels_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"}
            )
        else:  # CSV
            return app.response_class(
                data,
                mimetype='text/csv',
                headers={"Content-Disposition": f"attachment; filename=channels_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
            )
            
    except Exception as e:
        logger.error(f"Error exporting data: {e}")
        return jsonify({"error": "Błąd podczas eksportu danych"}), 500

@app.route('/api/admin/logs', methods=['GET'])
def get_admin_logs():
    """Get admin action logs"""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403
    
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)  # Max 100 per page
        offset = (page - 1) * per_page
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get total count
        cur.execute("SELECT COUNT(*) FROM admin_logs;")
        total_logs = cur.fetchone()[0]
        
        # Get paginated logs
        cur.execute("""
            SELECT action, target_type, target_id, details, ip_address, timestamp
            FROM admin_logs
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s;
        """, (per_page, offset))
        
        logs = []
        for row in cur.fetchall():
            logs.append({
                'action': row[0],
                'target_type': row[1],
                'target_id': row[2],
                'details': row[3],
                'ip_address': str(row[4]) if row[4] else None,
                'timestamp': row[5].isoformat() if row[5] else None
            })
        
        return jsonify({
            'logs': logs,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_logs,
                'pages': (total_logs + per_page - 1) // per_page
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting admin logs: {e}")
        return jsonify({"error": "Błąd podczas pobierania logów administratora"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels', methods=['GET'])
@rate_limit(max_requests=100, per_seconds=3600)  # 100 requests per hour
def get_channels():
    """Pobiera wszystkie kanały z bazy danych, sortując je według ostatniego boostowania,
    z wykluczeniem kanałów partnerskich."""
    conn = None
    cur = None
    try:
        # Get pagination parameters
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(max(1, int(request.args.get('per_page', 20))), 100)  # Limit to 100 per page
        offset = (page - 1) * per_page
        
        # Get search and filter parameters
        search = sanitize_text(request.args.get('search', ''), 100)
        category = sanitize_text(request.args.get('category', ''), 50)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Build WHERE clause
        where_conditions = ["is_partner = FALSE"]
        params = []
        
        if search:
            where_conditions.append("(LOWER(name) LIKE %s OR LOWER(description) LIKE %s)")
            search_pattern = f"%{search.lower()}%"
            params.extend([search_pattern, search_pattern])
        
        if category and category.lower() != 'all':
            where_conditions.append("LOWER(category) = %s")
            params.append(category.lower())
        
        where_clause = " AND ".join(where_conditions)
        
        # Get total count for pagination
        count_query = f"SELECT COUNT(*) FROM channels WHERE {where_clause};"
        cur.execute(count_query, params)
        total_channels = cur.fetchone()[0]
        
        # Get channels with pagination
        query = f"""
            SELECT id, name, description, link, rating, ratings_count, follower_count, 
                   profile_image_url, category, view_count
            FROM channels 
            WHERE {where_clause}
            ORDER BY last_boosted DESC NULLS LAST, created_at DESC
            LIMIT %s OFFSET %s;
        """
        params.extend([per_page, offset])
        cur.execute(query, params)
        channels_data = cur.fetchall()

        channels_list = []
        for channel in channels_data:
            channel_id = channel[0]
            
            # Increment view count
            cur.execute("UPDATE channels SET view_count = view_count + 1 WHERE id = %s;", (channel_id,))
            
            # Get comments for this channel
            cur.execute("SELECT id, author, text, created_at FROM comments WHERE channel_id = %s AND is_approved = TRUE ORDER BY created_at DESC;", (channel_id,))
            comments_data = cur.fetchall()
            comments_list = [{
                'id': c[0],
                'author': sanitize_text(c[1]),
                'text': sanitize_text(c[2]),
                'date': c[3].isoformat().split('T')[0]
            } for c in comments_data]

            channels_list.append({
                'id': channel_id,
                'name': sanitize_text(channel[1]),
                'description': sanitize_text(channel[2]),
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'followerCount': channel[6] if channel[6] is not None else 0,
                'profileImageUrl': channel[7],
                'category': sanitize_text(channel[8]) if channel[8] else 'Ogólne',
                'viewCount': channel[9] if channel[9] is not None else 0,
                'comments': comments_list
            })
        
        conn.commit()  # Commit view count updates
        
        return jsonify({
            'channels': channels_list,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_channels,
                'pages': (total_channels + per_page - 1) // per_page
            }
        })
        
    except ValueError as e:
        return jsonify({"error": "Nieprawidłowe parametry paginacji"}), 400
    except Exception as e:
        logger.error(f"Error loading channels: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/partner_channels', methods=['GET'])
@rate_limit(max_requests=100, per_seconds=3600)
def get_partner_channels():
    """Pobiera tylko kanały partnerskie z bazy danych."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Pobierz tylko kanały, które SĄ partnerami
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url, category, view_count FROM channels WHERE is_partner = TRUE ORDER BY created_at DESC;")
        channels_data = cur.fetchall()

        partner_channels_list = []
        for channel in channels_data:
            channel_id = channel[0]
            
            # Increment view count
            cur.execute("UPDATE channels SET view_count = view_count + 1 WHERE id = %s;", (channel_id,))
            
            cur.execute("SELECT id, author, text, created_at FROM comments WHERE channel_id = %s AND is_approved = TRUE ORDER BY created_at DESC;", (channel_id,))
            comments_data = cur.fetchall()
            comments_list = [{
                'id': c[0],
                'author': sanitize_text(c[1]),
                'text': sanitize_text(c[2]),
                'date': c[3].isoformat().split('T')[0]
            } for c in comments_data]

            partner_channels_list.append({
                'id': channel_id,
                'name': sanitize_text(channel[1]),
                'description': sanitize_text(channel[2]),
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'followerCount': channel[6] if channel[6] is not None else 0,
                'profileImageUrl': channel[7],
                'category': sanitize_text(channel[8]) if channel[8] else 'Ogólne',
                'viewCount': channel[9] if channel[9] is not None else 0,
                'comments': comments_list
            })
        
        conn.commit()  # Commit view count updates
        return jsonify(partner_channels_list)
        
    except Exception as e:
        logger.error(f"Error loading partner channels: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów partnerskich"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels', methods=['POST'])
@rate_limit(max_requests=10, per_seconds=3600)  # Limit channel creation
def add_channel():
    """Dodaje nowy kanał do bazy danych, próbując pobrać nazwę, opis i liczbę obserwujących z linku."""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Brak danych JSON"}), 400
    
    link = data.get('link', '').strip()
    frontend_name = sanitize_text(data.get('name', ''), 255)
    frontend_description = sanitize_text(data.get('description', ''), 1000)
    frontend_profile_image_url = data.get('profileImageUrl', '').strip()
    frontend_category = validate_category(data.get('category', 'Ogólne'))

    if not link:
        return jsonify({"error": "Pole 'link' jest wymagane."}), 400

    if not validate_url(link):
        return jsonify({"error": "Nieprawidłowy format URL."}), 400

    # Ulepszona walidacja linku WhatsApp
    if not is_valid_whatsapp_channel_link(link):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp. Upewnij się, że link zaczyna się od 'https://whatsapp.com/channel/' lub 'https://www.whatsapp.com/channel/' i zawiera prawidłowy identyfikator kanału (min. 24 znaki alfanumeryczne)."}), 400

    # Validate profile image URL if provided
    if frontend_profile_image_url and not validate_url(frontend_profile_image_url):
        return jsonify({"error": "Nieprawidłowy format URL obrazka profilowego."}), 400

    parsed_url = urlparse(link)

    # Próba scrapingu
    scraped_data = fetch_channel_details_from_whatsapp_page(link)
    
    # Walidacja: Jeśli nazwa to "Kanał WhatsApp", zablokuj dodawanie
    if scraped_data and scraped_data['name'] == "Kanał WhatsApp":
        return jsonify({"error": "Nie można dodać kanału o nazwie 'Kanał WhatsApp'. Prawdopodobnie link prowadzi do nieistniejącego lub ogólnego kanału."}), 400

    name = scraped_data['name'] if scraped_data and scraped_data['name'] else frontend_name
    description = scraped_data['description'] if scraped_data and scraped_data['description'] else frontend_description
    follower_count = scraped_data['follower_count'] if scraped_data else 0
    profile_image_url = scraped_data['profile_image_url'] if scraped_data and scraped_data['profile_image_url'] else frontend_profile_image_url
    category = frontend_category

    # Ostateczne fallbacki
    if not name:
        name = parsed_url.path.strip('/').split('/')[-1][:255]  # Limit length
    if not description:
        description = "Brak dostępnego opisu."
    
    # Additional validation
    if len(name) < 2:
        return jsonify({"error": "Nazwa kanału musi mieć co najmniej 2 znaki."}), 400
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if channel already exists
        cur.execute("SELECT id FROM channels WHERE link = %s;", (link,))
        if cur.fetchone():
            return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
        
        cur.execute(
            "INSERT INTO channels (name, description, link, follower_count, profile_image_url, category) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
            (name, description, link, follower_count, profile_image_url, category)
        )
        channel_id = cur.fetchone()[0]
        conn.commit()
        
        logger.info(f"New channel added: {name} (ID: {channel_id})")
        
        return jsonify({
            "message": "Kanał dodany pomyślnie",
            "id": channel_id,
            "name": name,
            "description": description,
            "followerCount": follower_count,
            "profileImageUrl": profile_image_url,
            "category": category
        }), 201
        
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        logger.warning(f"Attempted to add duplicate channel: {link}")
        return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
    except Exception as e:
        logger.error(f"Error adding channel: {e}")
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
    """Uwierzytelnia administratorów i zwraca token."""
    data = request.get_json()
    pass1 = data.get('passwordOne')
    pass2 = data.get('passwordTwo')

    if not ADMIN_PASS1 or not ADMIN_PASS2:
        return jsonify({'error': 'Hasła administratora nie są skonfigurowane na serwerze.'}), 500

    if pass1 == ADMIN_PASS1 and pass2 == ADMIN_PASS2:
        return jsonify({"message": "Pomyślne logowanie", "token": ADMIN_AUTH_TOKEN}), 200
    else:
        return jsonify({"error": "Nieprawidłowe hasła administratora."}), 401

@app.route('/api/admin/channels', methods=['GET'])
def get_admin_channels():
    """
    Pobiera wszystkie kanały z bazy danych, w tym status 'is_partner' i kategorię,
    dostępne tylko dla administratora.
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Pobierz WSZYSTKIE kanały dla panelu admina
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url, is_partner, category FROM channels ORDER BY created_at DESC;")
        channels_data = cur.fetchall()

        channels_list = []
        for channel in channels_data:
            channel_id = channel[0]
            cur.execute("SELECT id, author, text, created_at FROM comments WHERE channel_id = %s ORDER BY created_at DESC;", (channel_id,))
            comments_data = cur.fetchall()
            comments_list = [{
                'id': c[0], # Dodano ID komentarza
                'author': c[1],
                'text': c[2],
                'date': c[3].isoformat().split('T')[0]
            } for c in comments_data]

            channels_list.append({
                'id': channel_id,
                'name': channel[1],
                'description': channel[2],
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'followerCount': channel[6] if channel[6] is not None else 0,
                'profileImageUrl': channel[7],
                'is_partner': channel[8], # Dodano status is_partner
                'category': channel[9], # Dodano kategorię
                'comments': comments_list
            })
        return jsonify(channels_list)
    except Exception as e:
        print(f"ERROR: Błąd podczas pobierania kanałów dla administratora: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów dla administratora"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/admin/channels/<int:channel_id>', methods=['PUT'])
def update_channel(channel_id):
    """
    Aktualizuje szczegóły kanału (nazwę, opis, link, profilowe URL, kategorię).
    Dostępne tylko dla administratora.
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Brak danych JSON"}), 400
    
    name = sanitize_text(data.get('name', ''), 255)
    description = sanitize_text(data.get('description', ''), 1000)
    link = data.get('link', '').strip()
    profile_image_url = data.get('profileImageUrl', '').strip()
    category = validate_category(data.get('category', 'Ogólne'))

    if not all([name, description, link]):
        return jsonify({"error": "Wszystkie pola (nazwa, opis, link) są wymagane."}), 400

    if not validate_url(link):
        return jsonify({"error": "Nieprawidłowy format URL."}), 400

    if not is_valid_whatsapp_channel_link(link):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp."}), 400

    if profile_image_url and not validate_url(profile_image_url):
        return jsonify({"error": "Nieprawidłowy format URL obrazka profilowego."}), 400

    if len(name) < 2:
        return jsonify({"error": "Nazwa kanału musi mieć co najmniej 2 znaki."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if channel exists
        cur.execute("SELECT name FROM channels WHERE id = %s;", (channel_id,))
        old_channel = cur.fetchone()
        if not old_channel:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
        
        cur.execute(
            "UPDATE channels SET name = %s, description = %s, link = %s, profile_image_url = %s, category = %s, last_updated = CURRENT_TIMESTAMP WHERE id = %s RETURNING id;",
            (name, description, link, profile_image_url, category, channel_id)
        )
        updated_id = cur.fetchone()
        conn.commit()

        if updated_id:
            log_admin_action("UPDATE_CHANNEL", "channel", channel_id, 
                           f"Updated channel '{old_channel[0]}' to '{name}'")
            logger.info(f"Channel {channel_id} updated by admin")
            return jsonify({"message": f"Kanał o ID {channel_id} został pomyślnie zaktualizowany."}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
            
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
    except Exception as e:
        logger.error(f"Error updating channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas aktualizacji kanału."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/<int:channel_id>', methods=['DELETE'])
def delete_channel(channel_id):
    """Usuwa określony kanał z bazy danych (tylko dla administratora)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get channel name before deletion for logging
        cur.execute("SELECT name FROM channels WHERE id = %s;", (channel_id,))
        channel_data = cur.fetchone()
        
        if not channel_data:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
        
        channel_name = channel_data[0]
        
        cur.execute("DELETE FROM channels WHERE id = %s RETURNING id;", (channel_id,))
        deleted_id = cur.fetchone()
        conn.commit()

        if deleted_id:
            log_admin_action("DELETE_CHANNEL", "channel", channel_id, 
                           f"Deleted channel '{channel_name}'")
            logger.info(f"Channel {channel_id} ({channel_name}) deleted by admin")
            return jsonify({"message": f"Kanał o ID {channel_id} został pomyślnie usunięty."}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
            
    except Exception as e:
        logger.error(f"Error deleting channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas usuwania kanału."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/admin/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    """Usuwa określony komentarz z bazy danych (tylko dla administratora)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get comment details before deletion for logging
        cur.execute("SELECT text, author, channel_id FROM comments WHERE id = %s;", (comment_id,))
        comment_data = cur.fetchone()
        
        if not comment_data:
            return jsonify({"error": "Komentarz nie znaleziony."}), 404
        
        comment_text, author, channel_id = comment_data
        
        cur.execute("DELETE FROM comments WHERE id = %s RETURNING id;", (comment_id,))
        deleted_id = cur.fetchone()
        conn.commit()

        if deleted_id:
            log_admin_action("DELETE_COMMENT", "comment", comment_id, 
                           f"Deleted comment by '{author}' on channel {channel_id}")
            logger.info(f"Comment {comment_id} deleted by admin")
            return jsonify({"message": f"Komentarz o ID {comment_id} został pomyślnie usunięty."}), 200
        else:
            return jsonify({"error": "Komentarz nie znaleziony."}), 404
            
    except Exception as e:
        logger.error(f"Error deleting comment {comment_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas usuwania komentarza."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/all', methods=['DELETE'])
def clear_all_channels():
    """Usuwa wszystkie kanały i komentarze z bazy danych (tylko dla administratora)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get count before deletion for logging
        cur.execute("SELECT COUNT(*) FROM channels;")
        channel_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM comments;")
        comment_count = cur.fetchone()[0]
        
        # Kolejność ma znaczenie: najpierw komentarze ze względu na ograniczenie klucza obcego
        cur.execute("DELETE FROM comments;")
        cur.execute("DELETE FROM ratings;")
        cur.execute("DELETE FROM channels;")
        conn.commit()
        
        log_admin_action("CLEAR_ALL_CHANNELS", details=f"Deleted {channel_count} channels and {comment_count} comments")
        logger.warning(f"All channels and comments cleared by admin. {channel_count} channels, {comment_count} comments deleted.")
        
        return jsonify({"message": "Wszystkie kanały i komentarze zostały usunięte."}), 200
        
    except Exception as e:
        logger.error(f"Error clearing all channels: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas czyszczenia wszystkich kanałów."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/<int:channel_id>/rate', methods=['POST'])
@rate_limit(max_requests=10, per_seconds=3600)  # Limit ratings
def rate_channel(channel_id):
    """Przesyła ocenę dla kanału i aktualizuje jego średnią ocenę."""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Brak danych JSON"}), 400
    
    rating = data.get('rating')

    if not validate_rating(rating):
        return jsonify({"error": "Ocena musi być liczbą od 1 do 5."}), 400

    rating = int(rating)  # Convert to int after validation
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if channel exists
        cur.execute("SELECT rating, ratings_count FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()

        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        current_avg_rating = result[0] if result[0] is not None else 0.0
        current_ratings_count = result[1] if result[1] is not None else 0

        new_total_rating = (current_avg_rating * current_ratings_count) + rating
        new_ratings_count = current_ratings_count + 1
        new_avg_rating = new_total_rating / new_ratings_count

        # Update the database
        cur.execute(
            "UPDATE channels SET rating = %s, ratings_count = %s, last_updated = CURRENT_TIMESTAMP WHERE id = %s;",
            (new_avg_rating, new_ratings_count, channel_id)
        )
        
        # Store individual rating for analytics
        client_ip = request.remote_addr
        cur.execute(
            "INSERT INTO ratings (score, channel_id, ip_address) VALUES (%s, %s, %s);",
            (rating, channel_id, client_ip)
        )
        
        conn.commit()
        
        logger.info(f"Channel {channel_id} rated {rating}/5. New average: {new_avg_rating:.2f}")
        
        return jsonify({
            "message": "Ocena dodana pomyślnie",
            "new_average_rating": round(new_avg_rating, 2),
            "new_ratings_count": new_ratings_count
        }), 200
        
    except Exception as e:
        logger.error(f"Error rating channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania oceny"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/comments', methods=['POST'])
@rate_limit(max_requests=20, per_seconds=3600)  # Limit comments
def add_comment_to_channel(channel_id):
    """Dodaje komentarz do określonego kanału."""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Brak danych JSON"}), 400
    
    text = sanitize_text(data.get('text', ''), 500)  # Limit comment length
    author = sanitize_text(data.get('author', 'Anonimowy użytkownik'), 100)

    if not text or len(text.strip()) < 3:
        return jsonify({"error": "Komentarz musi mieć co najmniej 3 znaki."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if channel exists
        cur.execute("SELECT id FROM channels WHERE id = %s;", (channel_id,))
        if not cur.fetchone():
            return jsonify({"error": "Kanał nie znaleziony."}), 404
        
        # Add comment (with approval flag for moderation)
        cur.execute(
            "INSERT INTO comments (channel_id, author, text, is_approved) VALUES (%s, %s, %s, %s);",
            (channel_id, author, text, True)  # Auto-approve for now, can be changed for moderation
        )
        conn.commit()
        
        logger.info(f"Comment added to channel {channel_id} by {author}")
        
        return jsonify({"message": "Komentarz dodany pomyślnie"}), 201
        
    except Exception as e:
        logger.error(f"Error adding comment to channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas dodawania komentarza"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.route('/api/channels/<int:channel_id>/boost', methods=['POST'])
def boost_channel(channel_id):
    """Aktualizuje znacznik czasu ostatniego boostowania kanału na bieżący czas."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            "UPDATE channels SET last_boosted = CURRENT_TIMESTAMP WHERE id = %s RETURNING name;",
            (channel_id,)
        )
        boosted_channel_name = cur.fetchone()
        conn.commit()

        if boosted_channel_name:
            return jsonify({"message": f"Kanał '{boosted_channel_name[0]}' został pomyślnie zboostowany!"}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
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

@app.route('/api/admin/channels/<int:channel_id>/admin_boost', methods=['POST'])
def admin_boost_channel(channel_id):
    """
    Ustawia znacznik czasu ostatniego boostowania kanału na bieżący czas (bez cooldownu).
    Dostępne tylko dla administratora.
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            "UPDATE channels SET last_boosted = CURRENT_TIMESTAMP WHERE id = %s RETURNING name;",
            (channel_id,)
        )
        boosted_channel_name = cur.fetchone()
        conn.commit()

        if boosted_channel_name:
            return jsonify({"message": f"Kanał '{boosted_channel_name[0]}' został pomyślnie zboostowany przez administratora!"}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
    except Exception as e:
        print(f"ERROR: Błąd podczas boostowania kanału przez administratora {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas boostowania kanału przez administratora."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/<int:channel_id>/unboost', methods=['POST'])
def unboost_channel(channel_id):
    """
    Ustawia znacznik czasu 'last_boosted' na bardzo starą datę, aby przesunąć kanał na dół listy.
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
            "UPDATE channels SET last_boosted = %s WHERE id = %s RETURNING name;",
            (unboost_timestamp, channel_id)
        )
        unboosted_channel_name = cur.fetchone()
        
        if unboosted_channel_name:
            conn.commit()
            return jsonify({"message": f"Kanał '{unboosted_channel_name[0]}' został 'unboostowany'."}), 200
        else:
            conn.rollback() # Rollback if no channel was found
            return jsonify({"error": "Kanał nie znaleziony."}), 404
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

@app.route('/api/admin/channels/<int:channel_id>/admin_unboost', methods=['POST'])
def admin_unboost_channel(channel_id):
    """
    Ustawia znacznik czasu 'last_boosted' na bardzo starą datę (bez cooldownu).
    Dostępne tylko dla administratora.
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        unboost_timestamp = datetime(1970, 1, 1) # Unix Epoch
        cur.execute(
            "UPDATE channels SET last_boosted = %s WHERE id = %s RETURNING name;",
            (unboost_timestamp, channel_id)
        )
        unboosted_channel_name = cur.fetchone()
        conn.commit()

        if unboosted_channel_name:
            return jsonify({"message": f"Kanał '{unboosted_channel_name[0]}' został 'unboostowany' przez administratora."}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
    except Exception as e:
        print(f"ERROR: Błąd podczas unboostowania kanału przez administratora {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas unboostowania kanału przez administratora."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/channels/<int:channel_id>/toggle_partner', methods=['POST'])
def toggle_partner_status(channel_id):
    """
    Przełącza status 'is_partner' dla danego kanału (tylko dla administratora).
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Pobierz aktualny status is_partner i nazwę kanału
        cur.execute("SELECT is_partner, name FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()

        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        current_is_partner, channel_name = result
        new_is_partner = not current_is_partner # Przełącz status

        cur.execute(
            "UPDATE channels SET is_partner = %s, last_updated = CURRENT_TIMESTAMP WHERE id = %s RETURNING name, is_partner;",
            (new_is_partner, channel_id)
        )
        updated_channel_info = cur.fetchone()
        conn.commit()

        if updated_channel_info:
            status_text = "partnera" if new_is_partner else "zwykłego kanału"
            log_admin_action("TOGGLE_PARTNER", "channel", channel_id, 
                           f"Changed partner status of '{channel_name}' to {new_is_partner}")
            logger.info(f"Channel {channel_id} partner status changed to {new_is_partner}")
            
            return jsonify({
                "message": f"Status partnera dla kanału '{updated_channel_info[0]}' zmieniony na {status_text}.",
                "is_partner": updated_channel_info[1]
            }), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
            
    except Exception as e:
        logger.error(f"Error toggling partner status for channel {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas przełączania statusu partnera."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


if __name__ == '__main__':
    app.run(debug=True, port=5000)
