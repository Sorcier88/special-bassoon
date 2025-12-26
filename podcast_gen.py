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

# OBJECTIF : Nombre de SUCCÈS voulus par playlist
TARGET_SUCCESS_PER_PLAYLIST = 3
SEARCH_WINDOW_SIZE = 20

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
    """Upload un fichier. S'il existe déjà, on le SUPPRIME pour forcer la mise à jour."""
    if not os.path.exists(filename): return None
    print(f"Upload de {filename}...")
    
    # Vérification et SUPPRESSION de l'ancien fichier (corrompu ou ancien)
    for asset in release.get_assets():
        if asset.name == filename:
            print(f"   -> Ancienne version trouvée. Suppression...")
            asset.delete_asset()
            break
            
    # Upload du nouveau
    try:
        asset = release.upload_asset(filename)
        return asset.browser_download_url
    except Exception as e:
        print(f"Erreur upload {filename}: {e}")
        return None

def process_video_download(entry, ydl, release, fg, custom_image, custom_author, current_log_file):
    vid_id = entry['id']
    vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    print(f"-> Traitement : {entry.get('title', vid_id)}")

    # 1. Téléchargement (Avec vérification intégrité stricte)
    info = ydl.extract_info(vid_url, download=True)
    
    if not info: 
        raise Exception("Echec silencieux (Info vide)")

    mp3_filename = f"{vid_id}.mp3"
    
    # Recherche miniature
    thumb_files = glob.glob(f"{vid_id}.*")
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    jpg_filename = None
    for f in thumb_files:
        if any(f.lower().endswith(ext) for ext in image_extensions):
            jpg_filename = f; break
    
    # Vérification taille (Si < 10ko, c'est sûrement un fichier corrompu)
    if not os.path.exists(mp3_filename) or os.path.getsize(mp3_filename) < 10000:
        raise Exception("Fichier MP3 invalide ou trop petit")

    # 2. Upload (Avec écrasement automatique grâce à la modif)
    mp3_url = upload_asset(release, mp3_filename)
    if not mp3_url:
        raise Exception("Echec Upload GitHub")

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
        # 0. Setup
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        if cookies_env:
            print("Cookies chargés.")
            with open(COOKIE_FILE, 'w') as f:
                f.write(cookies_env)
        
        if proxy_url:
            print(f"Mode TOR activé: {proxy_url}")
        
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
            'quiet': False, 'ignoreerrors': True, 'no_warnings': True, 'socket_timeout': 60,
        }
        if proxy_url: base_opts['proxy'] = proxy_url
        if os.path.exists(COOKIE_FILE): base_opts['cookiefile'] = COOKIE_FILE

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
            print("Scan rapide des manquants...")
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                try:
                    info_scan = ydl_scan.extract_info(playlist_url, download=False)
                    if info_scan and 'entries' in info_scan:
                        for entry in info_scan['entries']:
                            # Si l'ID a été retiré du fichier log manuellement, il sera détecté ici comme "manquant"
                            if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                missing_entries.append(entry)
                except Exception as e: 
                    print(f"Erreur scan: {e}")
                    continue

            print(f"Total à traiter : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # SETUP RSS (inchangé)
            fg = FeedGenerator(); fg.load_extension('podcast')
            rss_loaded = False
            if os.path.exists(rss_filename):
                try: fg.parse_file(rss_filename); rss_loaded = True
                except: pass
            if not rss_loaded:
                fg.title(f'Podcast {rss_filename}'); fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate'); fg.description('Auto-generated')

            final_title = custom_title if custom_title else f'Podcast {rss_filename}'
            fg.title(final_title)
            if custom_image: fg.podcast.itunes_image(custom_image); fg.image(url=custom_image, title=final_title, link=f'https://github.com/{REPO_NAME}')
            if custom_author: fg.author({'name': custom_author}); fg.podcast.itunes_author(custom_author)

            # SETUP DOWNLOAD
            dl_opts = base_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 'outtmpl': '%(id)s.%(ext)s', 'writethumbnail': True,
                # Intégrité stricte activée
                'retries': 20, 'fragment_retries': 20, 'skip_unavailable_fragments': False, 'abort_on_unavailable_fragment': True,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
                'sleep_interval': 10, 'max_sleep_interval': 30
            })
            if sb_categories: dl_opts['sponsorblock_remove'] = sb_categories

            success_count = 0
            retry_queue = []

            # --- PASSE 1 ---
            print(f"--- PASSE 1 : Recherche de {TARGET_SUCCESS_PER_PLAYLIST} succès ---")
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                for entry in batch_to_process:
                    if success_count >= TARGET_SUCCESS_PER_PLAYLIST:
                        print("Objectif atteint.")
                        break

                    vid_id = entry['id']
                    try:
                        process_video_download(entry, ydl, release, fg, custom_image, custom_author, current_log_file)
                        print("   [OK]")
                        success_count += 1
                        cleanup_files(vid_id)
                    except Exception as e:
                        err_str = str(e).lower()
                        print(f"   [ECHEC] {e}")
                        if "private video" in err_str: print("   -> Privée. On glisse.")
                        elif "deleted" in err_str: 
                            print("   -> Supprimée. Blacklist.")
                            with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                        else:
                            print("   -> Erreur technique. Retry queue.")
                            retry_queue.append(entry)
                        cleanup_files(vid_id)

            # --- PASSE 2 : RETRY ---
            if retry_queue and success_count < TARGET_SUCCESS_PER_PLAYLIST:
                print(f"\n--- PASSE 2 : Retry après pause ---")
                time.sleep(45)
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    for entry in retry_queue:
                        if success_count >= TARGET_SUCCESS_PER_PLAYLIST: break
                        vid_id = entry['id']
                        print(f"Retry -> {vid_id}")
                        try:
                            process_video_download(entry, ydl, release, fg, custom_image, custom_author, current_log_file)
                            print("   [OK] Rattrapage réussi !")
                            success_count += 1
                        except Exception as e:
                            print(f"   [ECHEC FINAL] {e}")
                        cleanup_files(vid_id)

            fg.rss_file(rss_filename)
            print(f"Sauvegarde XML {rss_filename}")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()