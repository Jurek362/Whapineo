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
                last_boosted TIMESTAMP DEFAULT NULL
            );
        """)
        conn.commit() # Commit the table creation immediately

        # Spróbuj zmienić nazwę kolumny 'average_rating' na 'rating', jeśli istnieje
        try:
            # Używamy ALTER TABLE w oddzielnym bloku try-except i commitujemy go natychmiast
            # lub wykonujemy rollback, aby nie wpłynąć na dalsze operacje
            cur.execute("""
                ALTER TABLE channels RENAME COLUMN average_rating TO rating;
            """)
            conn.commit() # Commit the ALTER TABLE if successful
            print("INFO: Kolumna 'average_rating' w tabeli 'channels' została pomyślnie zmieniona na 'rating'.")
        except psycopg2.ProgrammingError as e:
            # Rollback tylko dla tej operacji ALTER TABLE, jeśli była w otwartej transakcji
            # (chociaż lepiej, aby ALTER TABLE były auto-commitowane lub w osobnym połączeniu)
            # W przypadku ProgrammingError, która nie blokuje całej transakcji, nie zawsze jest potrzebny rollback
            # ale dla bezpieczeństwa, zwłaszcza przy zmianach schematu.
            if conn and not conn.autocommit: # Tylko jeśli nie jest w autocommit
                 conn.rollback() # Rollback tylko tej operacji
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
            # Po operacji ALTER TABLE, stan połączenia powinien być normalny
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
            # Na Renderze, niepowodzenie połączenia z bazą danych na starcie często
            # oznacza, że aplikacja nie będzie działać poprawnie.
            # Tutaj możesz zdecydować, czy chcesz zwrócić błąd HTTP, aby uniemożliwić dalsze działanie.
            # np. abort(500)
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
    """Pobiera wszystkie kanały z bazy danych, sortując je według ostatniego boostowania."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Sortowanie: najpierw kanały z ostatnim boostem, potem według daty utworzenia
        # Używamy kolumny 'rating'
        cur.execute("SELECT id, name, description, link, rating, ratings_count, follower_count, profile_image_url FROM channels ORDER BY last_boosted DESC NULLS LAST, created_at DESC;")
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
                'profileImageUrl': channel[7],
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

if __name__ == '__main__':
    app.run(debug=True, port=5000)
