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

# Global flag to ensure database initialization runs only once
db_initialized = False

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


def log_activity(action_type, target_type, target_id=None, admin_token=None, details=None, old_value=None, new_value=None):
    """Logs admin activity to the activity_logs table."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO activity_logs (action_type, target_type, target_id, admin_token, details, old_value, new_value)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (action_type, target_type, target_id, admin_token[:20] if admin_token else None, details, old_value, new_value))
        conn.commit()
    except Exception as e:
        print(f"ERROR: Failed to log activity: {e}")
        if conn:
            conn.rollback()
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_db_connection():
    """Ustanawia i zwraca połączenie z bazą danych PostgreSQL."""
    if not DATABASE_URL:
        print("ERROR: Zmienna środowiskowa DATABASE_URL nie jest ustawiona. Skonfiguruj ją w zmiennych środowiskowych Render.com.")
        raise ValueError("Zmienna środowiskowa DATABASE_URL nie jest ustawiona.")
    try:
        # Upewnij się, że sslmode='require' jest używane dla Render.com
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"ERROR: Nie udało się połączyć z bazą danych przy użyciu DATABASE_URL: {DATABASE_URL[:30]}... (skrócone). Pełny błąd: {e}")
        # Re-raise the exception to indicate a critical startup failure
        raise

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
                rating REAL DEFAULT 0.0, -- Zmieniono z 'average_rating' na 'rating'
                ratings_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_boosted TIMESTAMP DEFAULT NULL,
                is_partner BOOLEAN DEFAULT FALSE, -- Kolumna: czy kanał jest partnerem
                country VARCHAR(255) DEFAULT 'General' -- RENAMED: channel country (was category)
            );
        """)
        conn.commit() # Commit the table creation immediately

        # Spróbuj zmienić nazwę kolumny 'average_rating' na 'rating', jeśli istnieje
        try:
            cur.execute("""
                ALTER TABLE channels RENAME COLUMN average_rating TO rating;
            """)
            conn.commit()
            print("INFO: Kolumna 'average_rating' w tabeli 'channels' została pomyślnie zmieniona na 'rating'.")
        except psycopg2.ProgrammingError as e:
            if conn and not conn.autocommit:
                 conn.rollback()
            if "column \"average_rating\" does not exist" in str(e):
                print("INFO: Kolumna 'average_rating' nie istnieje w tabeli 'channels', nie ma potrzeby zmiany nazwy.")
            elif "column \"rating\" already exists" in str(e) and "cannot rename" in str(e):
                print("INFO: Kolumna 'rating' już istnieje i kolumna 'average_rating' nie mogła zostać zmieniona (możliwa wcześniejsza migracja).")
            else:
                print(f"WARNING: Nieoczekiwany błąd podczas próby zmiany nazwy kolumny 'average_rating': {e}")
        except Exception as e:
            if conn and not conn.autocommit:
                conn.rollback()
            print(f"WARNING: Ogólny błąd podczas operacji ALTER TABLE: {e}")
        finally:
            pass

        # Dodaj kolumnę is_partner, jeśli nie istnieje
        try:
            cur.execute("""
                ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_partner BOOLEAN DEFAULT FALSE;
            """)
            conn.commit()
            print("INFO: Kolumna 'is_partner' w tabeli 'channels' została sprawdzona/dodana pomyślnie.")
        except Exception as e:
            if conn and not conn.autocommit:
                conn.rollback()
            print(f"WARNING: Błąd podczas dodawania kolumny 'is_partner': {e}")
        finally:
            pass

        # Dodaj kolumnę country, jeśli nie istnieje (renamed from category)
        try:
            cur.execute("""
                ALTER TABLE channels ADD COLUMN IF NOT EXISTS country VARCHAR(255) DEFAULT 'General';
            """)
            conn.commit()
            print("INFO: Kolumna 'country' w tabeli 'channels' została sprawdzona/dodana pomyślnie.")
        except Exception as e:
            if conn and not conn.autocommit:
                conn.rollback()
            print(f"WARNING: Błąd podczas dodawania kolumny 'country': {e}")
        finally:
            pass

        # Rename category column to country if it exists
        try:
            cur.execute("""
                ALTER TABLE channels RENAME COLUMN category TO country;
            """)
            conn.commit()
            print("INFO: Kolumna 'category' w tabeli 'channels' została pomyślnie zmieniona na 'country'.")
        except psycopg2.ProgrammingError as e:
            if conn and not conn.autocommit:
                 conn.rollback()
            if "column \"category\" does not exist" in str(e):
                print("INFO: Kolumna 'category' nie istnieje w tabeli 'channels', prawdopodobnie już została zmieniona na 'country'.")
            elif "column \"country\" already exists" in str(e):
                print("INFO: Kolumna 'country' już istnieje, nie ma potrzeby zmiany nazwy z 'category'.")
            else:
                print(f"WARNING: Nieoczekiwany błąd podczas próby zmiany nazwy kolumny 'category': {e}")
        except Exception as e:
            if conn and not conn.autocommit:
                conn.rollback()
            print(f"WARNING: Ogólny błąd podczas operacji ALTER TABLE: {e}")
        finally:
            pass


        # Utwórz inne tabele (comments i ratings), jeśli nie istnieją
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                author VARCHAR(255) DEFAULT 'Anonimowy użytkownik',
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                score INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE
            );
        """)
        
        # Create activity logs table for admin panel
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id SERIAL PRIMARY KEY,
                action_type VARCHAR(100) NOT NULL,
                target_type VARCHAR(50) NOT NULL,
                target_id INTEGER,
                admin_token VARCHAR(255),
                details TEXT,
                old_value TEXT,
                new_value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        conn.commit() # Commit pozostałych tabel
        print("INFO: Pozostałe tabele bazy danych zostały sprawdzone/utworzone pomyślnie.")
    except Exception as e:
        print(f"ERROR: Błąd podczas inicjalizacji schematu bazy danych: {e}")
        raise # Ponownie zgłoś wyjątek, aby wskazać krytyczny błąd uruchamiania
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
        except Exception as e:
            print(f"KRYTYCZNY BŁĄD: Nie udało się zainicjalizować bazy danych przy starcie aplikacji: {e}")
            pass


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
    """Obsługuje główną stronę HTML lub działa jako kontrola zdrowia."""
    return "Backend dla Whapineo działa. Dostęp do /api/channels, etc."

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Pobiera wszystkie kanały z bazy danych, sortując je według ostatniego boostowania,
    z wykluczeniem kanałów partnerskich."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Pobierz tylko kanały, które NIE są partnerami
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url, country FROM channels WHERE is_partner = FALSE ORDER BY last_boosted DESC NULLS LAST, created_at DESC;")
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
                'country': channel[8], # Zmieniono z category na country
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

@app.route('/api/partner_channels', methods=['GET'])
def get_partner_channels():
    """Pobiera tylko kanały partnerskie z bazy danych."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Pobierz tylko kanały, które SĄ partnerami
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url, country FROM channels WHERE is_partner = TRUE ORDER BY created_at DESC;")
        channels_data = cur.fetchall()

        partner_channels_list = []
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

            partner_channels_list.append({
                'id': channel_id,
                'name': channel[1],
                'description': channel[2],
                'link': channel[3],
                'rating': channel[4] if channel[4] is not None else 0.0,
                'ratingsCount': channel[5] if channel[5] is not None else 0,
                'followerCount': channel[6] if channel[6] is not None else 0,
                'profileImageUrl': channel[7],
                'country': channel[8], # Zmieniono z category na country
                'comments': comments_list
            })
        return jsonify(partner_channels_list)
    except Exception as e:
        print(f"ERROR: Błąd podczas pobierania kanałów partnerskich: {e}")
        return jsonify({"error": "Błąd podczas ładowania kanałów partnerskich"}), 500
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
    # New: frontend may send country
    frontend_country = data.get('country', 'General')


    if not link:
        return jsonify({"error": "Pole 'link' jest wymagane."}), 400

    # Użycie ulepszonej walidacji linku WhatsApp
    if not is_valid_whatsapp_channel_link(link):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp. Upewnij się, że link zaczyna się od 'https://whatsapp.com/channel/' lub 'https://www.whatsapp.com/channel/' i zawiera prawidłowy identyfikator kanału (min. 24 znaki alfanumeryczne)."}, {"link": link}), 400

    parsed_url = urlparse(link)

    # Próba scrapingu
    scraped_data = fetch_channel_details_from_whatsapp_page(link)
    
    # NOWA WALIDACJA: Jeśli nazwa to "Kanał WhatsApp", zablokuj dodawanie
    if scraped_data and scraped_data['name'] == "Kanał WhatsApp": # Zmieniono na "Kanał WhatsApp" by pasowało do fallabacku w fetch_channel_details
        return jsonify({"error": "Nie można dodać kanału o nazwie 'Kanał WhatsApp'. Prawdopodobnie link prowadzi do nieistniejącego lub ogólnego kanału."}), 400

    name = scraped_data['name'] if scraped_data and scraped_data['name'] else frontend_name # Użyj scraped, fallback do frontend
    description = scraped_data['description'] if scraped_data and scraped_data['description'] else frontend_description # Użyj scraped, fallback do frontend
    follower_count = scraped_data['follower_count'] if scraped_data else 0
    profile_image_url = scraped_data['profile_image_url'] if scraped_data and scraped_data['profile_image_url'] else frontend_profile_image_url
    country = frontend_country # Use country from frontend, if available, otherwise default

    # Ostateczne fallbacki, jeśli scraping i frontend nie dostarczyły danych
    if not name:
        name = parsed_url.path.strip('/').split('/')[-1] # Fallback to ID if scraping/frontend name fails
    if not description:
        description = "No description available."
    
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO channels (name, description, link, follower_count, profile_image_url, country) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
            (name, description, link, follower_count, profile_image_url, country)
        )
        channel_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({
            "message": "Kanał dodany pomyślnie",
            "id": channel_id,
            "name": name,
            "description": description,
            "followerCount": follower_count,
            "profileImageUrl": profile_image_url,
            "country": country
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
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url, is_partner, country FROM channels ORDER BY created_at DESC;")
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
                'country': channel[9], # Zmieniono z category na country
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
    Aktualizuje szczegóły kanału (nazwę, opis, link, profilowe URL, kraj).
    Dostępne tylko dla administratora.
    """
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    data = request.get_json()
    name = data.get('name')
    description = data.get('description')
    link = data.get('link')
    profile_image_url = data.get('profileImageUrl')
    country = data.get('country', 'General') # Use default if not provided

    if not all([name, description, link]):
        return jsonify({"error": "Wszystkie pola (nazwa, opis, link) są wymagane."}), 400

    if not is_valid_whatsapp_channel_link(link):
        return jsonify({"error": "Nieprawidłowy format linku kanału WhatsApp."}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get old values for logging
        cur.execute("SELECT name, description, link, profile_image_url, country FROM channels WHERE id = %s;", (channel_id,))
        old_data = cur.fetchone()
        if not old_data:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
            
        old_values = {
            'name': old_data[0],
            'description': old_data[1], 
            'link': old_data[2],
            'profile_image_url': old_data[3],
            'country': old_data[4]
        }
        
        cur.execute(
            "UPDATE channels SET name = %s, description = %s, link = %s, profile_image_url = %s, country = %s WHERE id = %s RETURNING id;",
            (name, description, link, profile_image_url, country, channel_id)
        )
        updated_id = cur.fetchone()
        conn.commit()

        if updated_id:
            # Log the activity
            new_values = {
                'name': name,
                'description': description,
                'link': link, 
                'profile_image_url': profile_image_url,
                'country': country
            }
            
            changes = []
            for key in old_values:
                if old_values[key] != new_values[key]:
                    changes.append(f"{key}: '{old_values[key]}' -> '{new_values[key]}'")
            
            if changes:
                log_activity(
                    action_type="UPDATE_CHANNEL",
                    target_type="channel", 
                    target_id=channel_id,
                    admin_token=request.headers.get('Authorization'),
                    details=f"Updated channel: {', '.join(changes)}",
                    old_value=str(old_values),
                    new_value=str(new_values)
                )
            
            return jsonify({"message": f"Kanał o ID {channel_id} został pomyślnie zaktualizowany."}), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        return jsonify({"error": "Kanał o podanym linku już istnieje."}), 409
    except Exception as e:
        print(f"ERROR: Błąd podczas aktualizacji kanału {channel_id}: {e}")
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
        cur.execute("DELETE FROM comments WHERE id = %s RETURNING id;", (comment_id,))
        deleted_id = cur.fetchone()
        conn.commit()

        if deleted_id:
            return jsonify({"message": f"Komentarz o ID {comment_id} został pomyślnie usunięty."}), 200
        else:
            return jsonify({"error": "Komentarz nie znaleziony."}), 404
    except Exception as e:
        print(f"ERROR: Błąd podczas usuwania komentarza {comment_id}: {e}")
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
        # Kolejność ma znaczenie: najpierw komentarze ze względu na ograniczenie klucza obcego, potem oceny
        cur.execute("DELETE FROM comments;")
        cur.execute("DELETE FROM ratings;")
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

        # Get current rating and count - teraz używamy kolumny 'rating'
        cur.execute("SELECT rating, ratings_count FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()

        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        current_avg_rating = result[0] if result[0] is not None else 0.0
        current_ratings_count = result[1] if result[1] is not None else 0

        new_total_rating = (current_avg_rating * current_ratings_count) + rating
        new_ratings_count = current_ratings_count + 1
        new_avg_rating = new_total_rating / new_ratings_count

        # Update the database - teraz używamy kolumny 'rating'
        cur.execute(
            "UPDATE channels SET rating = %s, ratings_count = %s WHERE id = %s;",
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
    author = data.get('author', 'Anonimowy użytkownik') # Domyślny autor, jeśli nie podany przez frontend

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

        # Pobierz aktualny status is_partner
        cur.execute("SELECT is_partner FROM channels WHERE id = %s;", (channel_id,))
        result = cur.fetchone()

        if not result:
            return jsonify({"error": "Kanał nie znaleziony."}), 404

        current_is_partner = result[0]
        new_is_partner = not current_is_partner # Przełącz status

        cur.execute(
            "UPDATE channels SET is_partner = %s WHERE id = %s RETURNING name, is_partner;",
            (new_is_partner, channel_id)
        )
        updated_channel_info = cur.fetchone()
        conn.commit()

        if updated_channel_info:
            return jsonify({
                "message": f"Status partnera dla kanału '{updated_channel_info[0]}' zmieniony na {updated_channel_info[1]}.",
                "is_partner": updated_channel_info[1]
            }), 200
        else:
            return jsonify({"error": "Kanał nie znaleziony."}), 404
    except Exception as e:
        print(f"ERROR: Błąd podczas przełączania statusu partnera dla kanału {channel_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas przełączania statusu partnera."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# --- New Enhanced Admin Endpoints ---

@app.route('/api/admin/statistics', methods=['GET'])
def get_admin_statistics():
    """Get statistics for admin dashboard."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Channels per country
        cur.execute("""
            SELECT country, COUNT(*) as count 
            FROM channels 
            GROUP BY country 
            ORDER BY count DESC;
        """)
        channels_per_country = [{'country': row[0], 'count': row[1]} for row in cur.fetchall()]
        
        # Comments per channel
        cur.execute("""
            SELECT c.name, COUNT(com.id) as comment_count
            FROM channels c
            LEFT JOIN comments com ON c.id = com.channel_id
            GROUP BY c.id, c.name
            ORDER BY comment_count DESC
            LIMIT 10;
        """)
        comments_per_channel = [{'channel': row[0], 'comments': row[1]} for row in cur.fetchall()]
        
        # Total statistics
        cur.execute("SELECT COUNT(*) FROM channels;")
        total_channels = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM comments;")
        total_comments = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM channels WHERE is_partner = TRUE;")
        partner_channels = cur.fetchone()[0]
        
        cur.execute("SELECT AVG(rating) FROM channels WHERE rating > 0;")
        avg_rating = cur.fetchone()[0] or 0
        
        statistics = {
            'total_channels': total_channels,
            'total_comments': total_comments,
            'partner_channels': partner_channels,
            'average_rating': round(float(avg_rating), 2),
            'channels_per_country': channels_per_country,
            'comments_per_channel': comments_per_channel
        }
        
        return jsonify(statistics)
        
    except Exception as e:
        print(f"ERROR: Error getting statistics: {e}")
        return jsonify({"error": "Błąd podczas pobierania statystyk"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/admin/activity-logs', methods=['GET'])
def get_activity_logs():
    """Get activity logs for admin panel."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403
        
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    offset = (page - 1) * per_page

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT action_type, target_type, target_id, details, created_at 
            FROM activity_logs 
            ORDER BY created_at DESC 
            LIMIT %s OFFSET %s;
        """, (per_page, offset))
        
        logs = []
        for row in cur.fetchall():
            logs.append({
                'action_type': row[0],
                'target_type': row[1], 
                'target_id': row[2],
                'details': row[3],
                'created_at': row[4].isoformat() if row[4] else None
            })
        
        # Get total count
        cur.execute("SELECT COUNT(*) FROM activity_logs;")
        total = cur.fetchone()[0]
        
        return jsonify({
            'logs': logs,
            'total': total,
            'page': page,
            'per_page': per_page,
            'has_next': offset + per_page < total
        })
        
    except Exception as e:
        print(f"ERROR: Error getting activity logs: {e}")
        return jsonify({"error": "Błąd podczas pobierania logów aktywności"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/admin/bulk-update-country', methods=['POST'])
def bulk_update_country():
    """Bulk update country for multiple channels."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    data = request.get_json()
    channel_ids = data.get('channel_ids', [])
    new_country = data.get('country')
    
    if not channel_ids or not new_country:
        return jsonify({"error": "channel_ids i country są wymagane"}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Update channels
        cur.execute("""
            UPDATE channels 
            SET country = %s 
            WHERE id = ANY(%s) 
            RETURNING id, name;
        """, (new_country, channel_ids))
        
        updated_channels = cur.fetchall()
        conn.commit()
        
        # Log the activity
        channel_names = [f"{row[1]} (ID: {row[0]})" for row in updated_channels]
        log_activity(
            action_type="BULK_UPDATE_COUNTRY",
            target_type="channels",
            admin_token=request.headers.get('Authorization'),
            details=f"Updated country to '{new_country}' for channels: {', '.join(channel_names)}",
            new_value=new_country
        )
        
        return jsonify({
            "message": f"Country updated for {len(updated_channels)} channels",
            "updated_count": len(updated_channels)
        }), 200
        
    except Exception as e:
        print(f"ERROR: Error in bulk country update: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas masowej aktualizacji kraju"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/admin/comments/<int:comment_id>', methods=['PUT'])
def update_comment(comment_id):
    """Update a comment (admin only)."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    data = request.get_json()
    new_text = data.get('text')
    
    if not new_text:
        return jsonify({"error": "Text jest wymagany"}), 400

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get old text for logging
        cur.execute("SELECT text FROM comments WHERE id = %s;", (comment_id,))
        old_data = cur.fetchone()
        if not old_data:
            return jsonify({"error": "Komentarz nie znaleziony"}), 404
            
        old_text = old_data[0]
        
        cur.execute(
            "UPDATE comments SET text = %s WHERE id = %s RETURNING id;",
            (new_text, comment_id)
        )
        updated_id = cur.fetchone()
        conn.commit()

        if updated_id:
            # Log the activity
            log_activity(
                action_type="UPDATE_COMMENT",
                target_type="comment",
                target_id=comment_id,
                admin_token=request.headers.get('Authorization'),
                details=f"Updated comment text",
                old_value=old_text,
                new_value=new_text
            )
            
            return jsonify({"message": f"Komentarz o ID {comment_id} został zaktualizowany."}), 200
        else:
            return jsonify({"error": "Komentarz nie znaleziony."}), 404
            
    except Exception as e:
        print(f"ERROR: Error updating comment {comment_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "Błąd podczas aktualizacji komentarza"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route('/api/admin/export/channels', methods=['GET'])
def export_channels():
    """Export channels data as CSV."""
    if not verify_admin_token(request.headers):
        return jsonify({"error": "Nieautoryzowany: Nieprawidłowy lub brakujący token"}), 403

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, name, description, link, rating, ratings_count, 
                   follower_count, profile_image_url, is_partner, country, 
                   created_at, last_boosted
            FROM channels 
            ORDER BY id;
        """)
        
        channels = cur.fetchall()
        
        # Create CSV content
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'ID', 'Name', 'Description', 'Link', 'Rating', 'Ratings Count',
            'Follower Count', 'Profile Image URL', 'Is Partner', 'Country',
            'Created At', 'Last Boosted'
        ])
        
        # Write data
        for channel in channels:
            writer.writerow([
                channel[0],  # id
                channel[1],  # name
                channel[2],  # description
                channel[3],  # link
                channel[4],  # rating
                channel[5],  # ratings_count
                channel[6],  # follower_count
                channel[7],  # profile_image_url
                channel[8],  # is_partner
                channel[9],  # country
                channel[10].isoformat() if channel[10] else '',  # created_at
                channel[11].isoformat() if channel[11] else ''   # last_boosted
            ])
        
        output.seek(0)
        csv_content = output.getvalue()
        
        # Log the activity
        log_activity(
            action_type="EXPORT_CHANNELS",
            target_type="channels",
            admin_token=request.headers.get('Authorization'),
            details=f"Exported {len(channels)} channels to CSV"
        )
        
        return jsonify({
            "csv_data": csv_content,
            "filename": f"channels_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        })
        
    except Exception as e:
        print(f"ERROR: Error exporting channels: {e}")
        return jsonify({"error": "Błąd podczas eksportu kanałów"}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


if __name__ == '__main__':
    app.run(debug=True, port=5000)
