import os
import json
import datetime
import yt_dlp
import time
import random
import glob
from github import Github
from feedgen.feed import FeedGenerator

# --- CONSTANTES ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
LOG_FILE = "downloaded_log.txt"
CONFIG_FILE = "playlists.json"
COOKIE_FILE = "cookies.txt"

# SÉCURITÉ : Nombre max de nouvelles vidéos à traiter par jour
MAX_DOWNLOADS_PER_RUN = 3 

def get_or_create_release(repo):
    try:
        return repo.get_release(RELEASE_TAG)
    except:
        return repo.create_git_release(tag=RELEASE_TAG, name="Audio Files", message="Stockage MP3", draft=False, prerelease=False)

def cleanup_files(vid_id):
    """Nettoie tous les fichiers temporaires (mp3, jpg, webp, json) liés à un ID"""
    for f in glob.glob(f"{vid_id}.*"):
        try:
            os.remove(f)
        except:
            pass

def run():
    total_downloads_session = 0

    # 0. Gestion Cookies
    cookies_env = os.environ.get('YOUTUBE_COOKIES')
    if cookies_env:
        print("Cookies chargés.")
        with open(COOKIE_FILE, 'w') as f:
            f.write(cookies_env)
    
    # 1. Chargement Config
    if not os.path.exists(CONFIG_FILE):
        print(f"Erreur: {CONFIG_FILE} introuvable.")
        return
    with open(CONFIG_FILE, 'r') as f:
        playlists_config = json.load(f)

    # 2. GitHub
    try:
        g = Github(os.environ['GITHUB_TOKEN'])
        repo = g.get_repo(REPO_NAME)
        release = get_or_create_release(repo)
    except Exception as e:
        print(f"Erreur GitHub: {e}")
        return

    # 3. Historique
    downloaded_ids = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            downloaded_ids = f.read().splitlines()

    # 4. Options yt-dlp (Mise à jour pour métadonnées et thumbnails)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(id)s.%(ext)s',
        # Télécharger la miniature pour l'incruster
        'writethumbnail': True, 
        'postprocessors': [
            # 1. Convertir en MP3
            {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'},
            # 2. Incruster la miniature dans le fichier MP3
            {'key': 'EmbedThumbnail'},
            # 3. Ajouter les métadonnées (Titre, Auteur, Description) dans le fichier MP3
            {'key': 'FFmpegMetadata', 'add_metadata': True}, 
        ],
        'quiet': False,
        'ignoreerrors': True,
        'no_warnings': True,
        'sleep_interval': 5,
        'max_sleep_interval': 15
    }
    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    # 5. Boucle Playlists
    for item in playlists_config:
        if total_downloads_session >= MAX_DOWNLOADS_PER_RUN:
            print("--- Limite journalière atteinte. ---")
            break

        rss_filename = item.get('filename')
        playlist_url = item.get('url')
        custom_title = item.get('podcast_name')
        custom_image = item.get('cover_image')
        custom_author = item.get('podcast_author') # NOUVEAU
        
        if not rss_filename or not playlist_url: continue

        print(f"\n--- Analyse : {rss_filename} ---")
        fg = FeedGenerator()
        fg.load_extension('podcast')
        
        # Charger RSS existant ou créer
        rss_loaded = False
        if os.path.exists(rss_filename):
            try: 
                fg.parse_file(rss_filename)
                rss_loaded = True
            except: 
                print("Fichier RSS existant corrompu ou illisible, création d'un nouveau.")
        
        if not rss_loaded:
            fg.title(f'Podcast {rss_filename}')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            fg.description('Auto-generated')

        # Configuration Podcast Global
        if custom_image:
            fg.podcast.itunes_image(custom_image)
            fg.image(url=custom_image, title=custom_title if custom_title else f'Podcast {rss_filename}', link=f'https://github.com/{REPO_NAME}')
        
        # NOUVEAU : Configuration de l'auteur
        if custom_author:
            fg.author({'name': custom_author})
            fg.podcast.itunes_author(custom_author)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(playlist_url, download=False)
                except Exception as e:
                    print(f"Erreur lecture playlist: {e}")
                    fg.rss_file(rss_filename)
                    continue

                if not info or 'entries' not in info: continue

                # Titre du Podcast
                if custom_title:
                    fg.title(custom_title)
                else:
                    fg.title(info.get('title', f'Podcast {rss_filename}'))

                for entry in info['entries']:
                    if total_downloads_session >= MAX_DOWNLOADS_PER_RUN: break
                    if not entry: continue
                    vid_id = entry['id']

                    if vid_id in downloaded_ids: continue

                    print(f"Traitement ({total_downloads_session + 1}/{MAX_DOWNLOADS_PER_RUN}) : {entry.get('title', vid_id)}")

                    try:
                        # Téléchargement + Conversion + Incrustation
                        ydl.download([entry['webpage_url']])
                        mp3_filename = f"{vid_id}.mp3"
                        
                        if not os.path.exists(mp3_filename):
                            print("Echec DL.")
                            cleanup_files(vid_id) # Nettoyage si échec partiel
                            continue

                        # Upload GitHub
                        asset_exists = False
                        for asset in release.get_assets():
                            if asset.name == mp3_filename:
                                asset_exists = True
                                download_url = asset.browser_download_url
                                break
                        
                        if not asset_exists:
                            asset = release.upload_asset(mp3_filename)
                            download_url = asset.browser_download_url
                        
                        # --- AJOUT AU FLUX RSS ---
                        fe = fg.add_entry()
                        fe.id(vid_id)
                        # Titre de la vidéo comme titre d'épisode
                        fe.title(entry['title']) 
                        # Description de la vidéo comme description d'épisode
                        fe.description(entry.get('description', 'Pas de description'))
                        
                        # Date de publication : Date de la vidéo ou maintenant par défaut
                        upload_date_str = entry.get('upload_date')
                        if upload_date_str:
                            try:
                                # yt-dlp renvoie YYYYMMDD, on le convertit en objet datetime UTC
                                pub_date = datetime.datetime.strptime(upload_date_str, '%Y%m%d').replace(tzinfo=datetime.timezone.utc)
                                fe.pubDate(pub_date)
                            except:
                                fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                        else:
                            fe.pubDate(datetime.datetime.now(datetime.timezone.utc))

                        fe.enclosure(download_url, 0, 'audio/mpeg')
                        
                        # Auteur pour l'épisode spécifique aussi
                        if custom_author:
                            fe.podcast.itunes_author(custom_author)

                        # Sauvegarde Log
                        with open(LOG_FILE, "a") as log:
                            log.write(f"{vid_id}\n")
                        downloaded_ids.append(vid_id)
                        
                        # Nettoyage fichiers locaux (MP3 + JPG/WEBP thumbnails)
                        cleanup_files(vid_id)
                        
                        total_downloads_session += 1
                        
                    except Exception as e:
                        print(f"Erreur vidéo {vid_id}: {e}")
                        cleanup_files(vid_id)

        except Exception as e:
            print(f"Erreur playlist: {e}")

        fg.rss_file(rss_filename)
        print(f"Sauvegarde XML {rss_filename}")

    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
