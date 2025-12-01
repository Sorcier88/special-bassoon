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
    """Nettoie tous les fichiers temporaires"""
    for f in glob.glob(f"{vid_id}*"):
        try:
            os.remove(f)
        except:
            pass

def upload_asset(release, filename):
    """Upload un fichier vers la Release GitHub et retourne son URL"""
    if not os.path.exists(filename):
        return None
        
    print(f"Upload de {filename}...")
    
    # Vérifier si l'asset existe déjà
    for asset in release.get_assets():
        if asset.name == filename:
            return asset.browser_download_url
            
    # Sinon on upload
    try:
        asset = release.upload_asset(filename)
        return asset.browser_download_url
    except Exception as e:
        print(f"Erreur upload {filename}: {e}")
        return None

def run():
    total_downloads_session = 0

    try:
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

        # 4. Options yt-dlp
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': '%(id)s.%(ext)s',
            'writethumbnail': True, 
            'postprocessors': [
                # Convertir la miniature en JPG (compatible Podcast)
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'},
                {'key': 'EmbedThumbnail'},
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
                break

            rss_filename = item.get('filename')
            playlist_url = item.get('url')
            custom_title = item.get('podcast_name')
            custom_image = item.get('cover_image')
            custom_author = item.get('podcast_author')
            
            if not rss_filename or not playlist_url: continue

            print(f"\n--- Analyse : {rss_filename} ---")
            fg = FeedGenerator()
            fg.load_extension('podcast')
            
            # Gestion RSS existant
            rss_loaded = False
            if os.path.exists(rss_filename):
                try: 
                    fg.parse_file(rss_filename)
                    rss_loaded = True
                except: pass
            
            if not rss_loaded:
                fg.title(f'Podcast {rss_filename}')
                fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
                fg.description('Auto-generated')

            if custom_image:
                fg.podcast.itunes_image(custom_image)
                fg.image(url=custom_image, title=custom_title, link=f'https://github.com/{REPO_NAME}')
            
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

                    if custom_title:
                        fg.title(custom_title)
                    else:
                        fg.title(info.get('title', f'Podcast {rss_filename}'))

                    for entry in info['entries']:
                        if total_downloads_session >= MAX_DOWNLOADS_PER_RUN: break
                        if not entry: continue
                        vid_id = entry['id']

                        if vid_id in downloaded_ids: continue

                        print(f"Traitement : {entry.get('title', vid_id)}")

                        try:
                            ydl.download([entry['webpage_url']])
                            
                            mp3_filename = f"{vid_id}.mp3"
                            # yt-dlp convertit la miniature en .jpg grâce à l'option FFmpegThumbnailsConvertor
                            jpg_filename = f"{vid_id}.jpg" 
                            
                            if not os.path.exists(mp3_filename):
                                cleanup_files(vid_id)
                                continue

                            # 1. Upload MP3
                            mp3_url = upload_asset(release, mp3_filename)
                            if not mp3_url:
                                cleanup_files(vid_id)
                                continue

                            # 2. Upload Image (Miniature)
                            thumb_url = upload_asset(release, jpg_filename)
                            
                            # 3. Création entrée RSS
                            fe = fg.add_entry()
                            fe.id(vid_id)
                            fe.title(entry['title']) 
                            fe.description(entry.get('description', 'Pas de description'))
                            
                            # Date
                            upload_date_str = entry.get('upload_date')
                            if upload_date_str:
                                try:
                                    pub_date = datetime.datetime.strptime(upload_date_str, '%Y%m%d').replace(tzinfo=datetime.timezone.utc)
                                    fe.pubDate(pub_date)
                                except:
                                    fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                            else:
                                fe.pubDate(datetime.datetime.now(datetime.timezone.utc))

                            fe.enclosure(mp3_url, 0, 'audio/mpeg')
                            
                            # --- IMAGE SPÉCIFIQUE À L'ÉPISODE ---
                            # Si on a réussi à uploader la miniature, on l'ajoute au RSS
                            if thumb_url:
                                fe.podcast.itunes_image(thumb_url)
                            elif custom_image:
                                # Sinon on remet l'image par défaut pour être sûr
                                fe.podcast.itunes_image(custom_image)

                            if custom_author:
                                fe.podcast.itunes_author(custom_author)

                            with open(LOG_FILE, "a") as log:
                                log.write(f"{vid_id}\n")
                            downloaded_ids.append(vid_id)
                            
                            cleanup_files(vid_id)
                            total_downloads_session += 1
                            
                        except Exception as e:
                            print(f"Erreur traitement {vid_id}: {e}")
                            cleanup_files(vid_id)

            except Exception as e:
                print(f"Erreur globale: {e}")

            fg.rss_file(rss_filename)
            print(f"Sauvegarde XML {rss_filename}")

    finally:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
            print("Nettoyage sécurité effectué.")

if __name__ == "__main__":
    run()
