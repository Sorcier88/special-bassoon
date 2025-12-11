import os
import json
import datetime
import yt_dlp
import time
import glob
import base64
import zipfile
import shutil
from github import Github
from feedgen.feed import FeedGenerator

# --- CONSTANTES ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
CONFIG_FILE = "playlists.json"
CACHE_DIR = "yt_cache"

# SÉCURITÉ
MAX_DOWNLOADS_PER_PLAYLIST = 2

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

def setup_oauth_cache():
    b64_cache = os.environ.get('YOUTUBE_OAUTH_CACHE_B64')
    if not b64_cache: return False
    try:
        if os.path.exists(CACHE_DIR): shutil.rmtree(CACHE_DIR)
        os.makedirs(CACHE_DIR)
        zip_path = "cache_restored.zip"
        with open(zip_path, "wb") as f: f.write(base64.b64decode(b64_cache))
        with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(CACHE_DIR)
        os.remove(zip_path)
        return True
    except: return False

def process_video(entry, ydl, release, fg, custom_image, custom_author, current_log_file):
    """Fonction unique pour traiter une vidéo (Téléchargement -> Upload -> RSS)"""
    vid_id = entry['id']
    vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    print(f"-> {entry.get('title', vid_id)}")

    # 1. Téléchargement
    info = ydl.extract_info(vid_url, download=True)
    if not info: return False # Echec

    mp3_filename = f"{vid_id}.mp3"
    
    # Recherche miniature
    thumb_files = glob.glob(f"{vid_id}.*")
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    jpg_filename = None
    for f in thumb_files:
        if any(f.lower().endswith(ext) for ext in image_extensions):
            jpg_filename = f
            break
    
    if not os.path.exists(mp3_filename):
        return False

    # 2. Upload
    mp3_url = upload_asset(release, mp3_filename)
    if not mp3_url:
        return False

    thumb_url = None
    if jpg_filename:
        thumb_url = upload_asset(release, jpg_filename)

    # 3. RSS
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
    if custom_author: fe.podcast.itunes_author(custom_author)

    # 4. Log succès
    with open(current_log_file, "a") as log:
        log.write(f"{vid_id}\n")
    
    return True

def run():
    try:
        # Init
        has_auth = setup_oauth_cache()
        proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, 'r') as f: playlists_config = json.load(f)

        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e:
            print(f"Erreur GitHub: {e}"); return

        # Options YT-DLP
        base_opts = {
            'quiet': False, 'ignoreerrors': True, 'no_warnings': True, 'socket_timeout': 30,
        }
        if proxy_url: base_opts['proxy'] = proxy_url
        if has_auth:
            base_opts['username'] = 'oauth2'
            base_opts['cache_dir'] = CACHE_DIR

        # Boucle Playlists
        for item in playlists_config:
            rss_filename = item.get('filename')
            playlist_url = item.get('url')
            custom_title = item.get('podcast_name')
            custom_image = item.get('cover_image')
            custom_author = item.get('podcast_author')
            sb_categories = item.get('sponsorblock_categories')
            
            if not rss_filename or not playlist_url: continue

            print(f"\n=== Playlist : {rss_filename} ===")
            current_log_file = f"log_{rss_filename}.txt"
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # SCAN
            missing_entries = []
            scan_opts = base_opts.copy(); scan_opts['extract_flat'] = True
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                try:
                    info_scan = ydl_scan.extract_info(playlist_url, download=False)
                    if info_scan and 'entries' in info_scan:
                        for entry in info_scan['entries']:
                            if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                missing_entries.append(entry)
                except: continue

            print(f"Manquants : {len(missing_entries)}")
            batch_to_process = missing_entries[:MAX_DOWNLOADS_PER_PLAYLIST]

            # SETUP RSS
            fg = FeedGenerator(); fg.load_extension('podcast')
            rss_loaded = False
            if os.path.exists(rss_filename):
                try: fg.parse_file(rss_filename); rss_loaded = True
                except: pass
            if not rss_loaded:
                fg.title(f'Podcast {rss_filename}')
                fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
                fg.description('Auto-generated')

            final_title = custom_title if custom_title else f'Podcast {rss_filename}'
            fg.title(final_title)
            if custom_image: fg.podcast.itunes_image(custom_image); fg.image(url=custom_image, title=final_title, link=f'https://github.com/{REPO_NAME}')
            if custom_author: fg.author({'name': custom_author}); fg.podcast.itunes_author(custom_author)

            # SETUP DOWNLOAD OPTION
            dl_opts = base_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 'outtmpl': '%(id)s.%(ext)s', 'writethumbnail': True,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
                'sleep_interval': 5, 'max_sleep_interval': 15
            })
            if sb_categories: dl_opts['sponsorblock_remove'] = sb_categories

            # --- LISTE DE RETRY ---
            retry_list = []

            # --- PASSE 1 ---
            print("--- PASSE 1 : Tentative standard ---")
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                for entry in batch_to_process:
                    vid_id = entry['id']
                    try:
                        success = process_video(entry, ydl, release, fg, custom_image, custom_author, current_log_file)
                        if not success:
                            # Si échec, on analyse l'erreur est difficile ici car yt-dlp capture l'exception en interne avec ignoreerrors=True
                            # Mais comme 'success' est False, on assume que c'est raté.
                            # On ne sait pas encore si c'est fatal ou pas sans le message d'erreur.
                            # Astuce : On va relancer sans try/catch dans la passe 2 pour voir l'erreur.
                            # Pour l'instant on ajoute tout échec à la liste de retry.
                            print(f"Echec Passe 1 pour {vid_id}. Ajout à la liste de retry.")
                            retry_list.append(entry)
                    except Exception as e:
                        # Ici on capture les erreurs fatales directes
                        err_str = str(e).lower()
                        # LISTE DES ERREURS FATALES (On blacklist tout de suite)
                        if "private video" in err_str or "deleted" in err_str or "account associated with this video has been terminated" in err_str:
                            print(f"ERREUR FATALE ({vid_id}): Vidéo HS. Blacklistée.")
                            with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                        else:
                            # Erreur potentiellement temporaire (Sign in, Geo, Network)
                            print(f"Erreur temporaire ({vid_id}): {e}. Ajout à la liste de retry.")
                            retry_list.append(entry)
                    
                    cleanup_files(vid_id)

            # --- PASSE 2 (RETRY) ---
            if retry_list:
                print(f"\n--- PASSE 2 : Retry de {len(retry_list)} vidéos (Pause 30s) ---")
                time.sleep(30) # Pause pour laisser respirer Tor ou l'API

                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    for entry in retry_list:
                        vid_id = entry['id']
                        print(f"Retry -> {vid_id}")
                        try:
                            # On tente le process
                            success = process_video(entry, ydl, release, fg, custom_image, custom_author, current_log_file)
                            if success:
                                print(f"SUCCES au 2ème essai !")
                            else:
                                print(f"ECHEC définitif pour ce run. Sera retenté demain.")
                                # ON NE LOGGUE PAS -> Donc ce sera retenté demain
                        except Exception as e:
                            # Même logique : si erreur fatale on blacklist, sinon on laisse couler
                            err_str = str(e).lower()
                            if "private video" in err_str or "deleted" in err_str:
                                print(f"ERREUR FATALE détectée au retry. Blacklist.")
                                with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                            else:
                                print(f"Toujours en erreur ({e}). Abandon pour aujourd'hui.")

            fg.rss_file(rss_filename)
            print(f"Sauvegarde XML {rss_filename}")

    finally:
        if os.path.exists(CACHE_DIR): shutil.rmtree(CACHE_DIR)

if __name__ == "__main__":
    run()
