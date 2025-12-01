import os
import json
import datetime
import yt_dlp
import time
import glob
from github import Github
from feedgen.feed import FeedGenerator

# --- CONSTANTES ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
CONFIG_FILE = "playlists.json"
COOKIE_FILE = "cookies.txt"

# SÉCURITÉ : Nombre max de téléchargements PAR PLAYLIST (et non plus au total)
# Cela garantit que toutes les playlists avancent, même si la première est très chargée.
MAX_DOWNLOADS_PER_PLAYLIST = 3

def get_or_create_release(repo):
    try:
        return repo.get_release(RELEASE_TAG)
    except:
        return repo.create_git_release(tag=RELEASE_TAG, name="Audio Files", message="Stockage MP3", draft=False, prerelease=False)

def cleanup_files(vid_id):
    for f in glob.glob(f"{vid_id}*"):
        try: os.remove(f)
        except: pass

def upload_asset(release, filename):
    if not os.path.exists(filename): return None
    print(f"Upload de {filename}...")
    for asset in release.get_assets():
        if asset.name == filename:
            return asset.browser_download_url
    try:
        asset = release.upload_asset(filename)
        return asset.browser_download_url
    except Exception as e:
        print(f"Erreur upload {filename}: {e}")
        return None

def run():
    try:
        # 0. Cookies
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        if cookies_env:
            print("Cookies chargés.")
            with open(COOKIE_FILE, 'w') as f:
                f.write(cookies_env)
        
        # 1. Config
        if not os.path.exists(CONFIG_FILE): return
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

        # 4. Options yt-dlp pour le TÉLÉCHARGEMENT
        ydl_opts_download = {
            'format': 'bestaudio/best',
            'outtmpl': '%(id)s.%(ext)s',
            'writethumbnail': True, 
            'postprocessors': [
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
            ydl_opts_download['cookiefile'] = COOKIE_FILE

        # 5. Boucle Playlists
        for item in playlists_config:
            rss_filename = item.get('filename')
            playlist_url = item.get('url')
            custom_title = item.get('podcast_name')
            custom_image = item.get('cover_image')
            custom_author = item.get('podcast_author')
            
            if not rss_filename or not playlist_url: continue

            print(f"\n=== Traitement Playlist : {rss_filename} ===")
            
            # GESTION DU LOG SÉPARÉ (Ex: log_tech.xml.txt)
            # On crée un fichier log unique pour cette playlist spécifique
            current_log_file = f"log_{rss_filename}.txt"
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f:
                    downloaded_ids = f.read().splitlines()

            # PHASE 1 : SCAN RAPIDE
            missing_entries = []
            scan_title = None 
            
            print(f"Recherche des nouveaux épisodes (Log: {current_log_file})...")
            with yt_dlp.YoutubeDL({'extract_flat': True, 'quiet': True, 'ignoreerrors': True}) as ydl_scan:
                try:
                    info_scan = ydl_scan.extract_info(playlist_url, download=False)
                    if info_scan:
                        scan_title = info_scan.get('title')
                        if 'entries' in info_scan:
                            for entry in info_scan['entries']:
                                if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                    missing_entries.append(entry)
                except Exception as e:
                    print(f"Erreur scan: {e}")
                    continue

            print(f"Vidéos manquantes trouvées : {len(missing_entries)}")
            
            # Sélection du lot à traiter (MAX_DOWNLOADS_PER_PLAYLIST)
            # On prend les 3 premiers manquants pour CETTE playlist
            batch_to_process = missing_entries[:MAX_DOWNLOADS_PER_PLAYLIST]
            
            # PRÉPARATION RSS
            fg = FeedGenerator()
            fg.load_extension('podcast')
            
            # Chargement existant ou création
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

            # --- APPLICATION DES INFO PERSONNALISÉES ---
            final_podcast_title = custom_title if custom_title else (scan_title if scan_title else f'Podcast {rss_filename}')
            fg.title(final_podcast_title)

            if custom_image:
                fg.podcast.itunes_image(custom_image)
                fg.image(url=custom_image, title=final_podcast_title, link=f'https://github.com/{REPO_NAME}')
            
            if custom_author:
                fg.author({'name': custom_author})
                fg.podcast.itunes_author(custom_author)

            # PHASE 2 : TÉLÉCHARGEMENT DU LOT
            if batch_to_process:
                print(f"Téléchargement de {len(batch_to_process)} vidéos pour cette playlist...")
                
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
                    for entry in batch_to_process:
                        vid_id = entry['id']
                        vid_url = entry.get('url') or entry.get('webpage_url') or f"https://www.youtube.com/watch?v={vid_id}"
                        
                        print(f"-> {entry.get('title', vid_id)}")

                        try:
                            # Full Metadata + DL
                            info = ydl.extract_info(vid_url, download=True)
                            
                            mp3_filename = f"{vid_id}.mp3"
                            jpg_filename = f"{vid_id}.jpg" 
                            
                            if not os.path.exists(mp3_filename):
                                cleanup_files(vid_id)
                                continue

                            mp3_url = upload_asset(release, mp3_filename)
                            if not mp3_url:
                                cleanup_files(vid_id)
                                continue

                            thumb_url = upload_asset(release, jpg_filename)
                            
                            # Création épisode RSS
                            fe = fg.add_entry()
                            fe.id(vid_id)
                            fe.title(info.get('title', entry.get('title', vid_id)))
                            fe.description(info.get('description', ''))
                            
                            upload_date_str = info.get('upload_date')
                            if upload_date_str:
                                try:
                                    pub_date = datetime.datetime.strptime(upload_date_str, '%Y%m%d').replace(tzinfo=datetime.timezone.utc)
                                    fe.pubDate(pub_date)
                                except:
                                    fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
                            else:
                                fe.pubDate(datetime.datetime.now(datetime.timezone.utc))

                            fe.enclosure(mp3_url, 0, 'audio/mpeg')
                            
                            if thumb_url: fe.podcast.itunes_image(thumb_url)
                            elif custom_image: fe.podcast.itunes_image(custom_image)
                            if custom_author: fe.podcast.itunes_author(custom_author)

                            # LOG SPÉCIFIQUE À LA PLAYLIST
                            with open(current_log_file, "a") as log:
                                log.write(f"{vid_id}\n")
                            # Pas besoin d'append à downloaded_ids car on reload à chaque tour de boucle
                            
                            cleanup_files(vid_id)
                            
                        except Exception as e:
                            print(f"Erreur traitement {vid_id}: {e}")
                            cleanup_files(vid_id)

            fg.rss_file(rss_filename)
            print(f"Sauvegarde XML {rss_filename}")

    finally:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
            print("Nettoyage effectué.")

if __name__ == "__main__":
    run()
