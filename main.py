import json
import os
import sqlite3
import sys
import requests
from bs4 import BeautifulSoup
from google import genai

# 1. Récupération des secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("Erreur : Clés API ou identifiants manquants dans les secrets GitHub.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# 2. Gestion Base de Données
def init_db():
    conn = sqlite3.connect("vu_articles.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def article_deja_traite(article_id):
    conn = sqlite3.connect("vu_articles.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM articles WHERE id = ?", (str(article_id),))
    existe = cursor.fetchone() is not None
    conn.close()
    return existe

def enregistrer_article(article_id):
    conn = sqlite3.connect("vu_articles.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO articles (id) VALUES (?)", (str(article_id),))
    conn.commit()
    conn.close()

# 3. Scraping Vinted (Approche native avec Requests & Headers)
def recupere_annonces_vinted(mots_cles="carhartt", max_items=10):
    print(f"Recherche sur Vinted FR : '{mots_cles}'...")
    annonces = []
    
    # Étape 3a : Obtenir les cookies de session (Obligatoire pour l'API)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    session = requests.Session()
    try:
        # Visiter la page d'accueil pour initialiser les cookies
        session.get("https://www.vinted.fr", headers=headers, timeout=10)
    except Exception as e:
        print(f"Impossible d'accéder à la page d'accueil Vinted : {e}")
        return []

    # Étape 3b : Requêter l'API de recherche Vinted
    api_url = "https://www.vinted.fr/api/v2/catalog/items"
    params = {
        "search_text": mots_cles,
        "order": "newest_first", # Les plus récents
        "per_page": max_items,
        "catalog": [] 
    }
    
    try:
        response = session.get(api_url, headers=headers, params=params, timeout=10)
        
        if response.status_code != 200:
            print(f"Erreur API Vinted (Status {response.status_code}) - Le serveur bloque peut-être GitHub Actions.")
            return []
            
        data = response.json()
        
        if "items" not in data:
            return []

        for item in data["items"]:
            # Les prix sont parfois renvoyés sous forme de string ou dict
            prix = item.get("price", {}).get("amount", 0) if isinstance(item.get("price"), dict) else item.get("price", 0)
            
            try:
                prix = float(prix)
            except (ValueError, TypeError):
                prix = 0.0

            annonces.append({
                "id": str(item.get("id")),
                "titre": item.get("title", ""),
                "marque": item.get("brand_title", ""),
                "prix": prix,
                "description": item.get("description", item.get("title", "")),
                "url": item.get("url", "")
            })
            
    except Exception as e:
        print(f"Erreur lors de l'extraction des JSON Vinted : {e}")
        
    return annonces

# 4. Évaluation IA
def evaluer_affaire(titre, prix, description, marque):
    prompt = f"""
    Tu es un expert de la revente Vinted.
    Analyse l'annonce :
    - Marque : {marque}
    - Titre : {titre}
    - Prix d'achat : {prix} €
    - Description : {description}

    Réponds EXCLUSIVEMENT sous forme d'un objet JSON strict :
    {{
      "est_bonne_affaire": true/false,
      "prix_estime_revente": float/int,
      "raison": "explication courte"
    }}
    """
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Erreur IA : {e}")
        return {"est_bonne_affaire": False}

# 5. Telegram
def envoyer_alerte_telegram(titre, prix, url, raison, prix_estime):
    message = (
        f"🚨 **ALERTE VINTED !**\n\n"
        f"📌 **Article :** {titre}\n"
        f"💰 **Prix :** {prix}€\n"
        f"📈 **Revente Est :** {prix_estime}€\n"
        f"💡 **IA :** {raison}\n\n"
        f"🔗 [Acheter]({url})"
    )
    tele_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(tele_url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    })

# 6. Boucle principale
def main():
    init_db()

    RECHERCHES = ["carhartt detroit", "stussy", "arcteryx"]
    MARQUES_VALIDES = ["carhartt", "stussy", "arc'teryx", "arcteryx"]

    for recherche in RECHERCHES:
        items = recupere_annonces_vinted(mots_cles=recherche, max_items=8)
        
        for item in items:
            if not item["id"] or article_deja_traite(item["id"]):
                continue

            enregistrer_article(item["id"])

            titre_marque = f"{item['titre']} {item['marque']}".lower()
            if not any(m in titre_marque for m in MARQUES_VALIDES) or item["prix"] <= 0 or item["prix"] > 80:
                continue

            print(f"Analyse IA : {item['titre']} - {item['prix']}€")

            analyse = evaluer_affaire(item["titre"], item["prix"], item["description"], item["marque"])

            if analyse.get("est_bonne_affaire"):
                envoyer_alerte_telegram(
                    item["titre"], item["prix"], item["url"],
                    analyse.get("raison", ""), analyse.get("prix_estime_revente", "")
                )

if __name__ == "__main__":
    main()