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

# OBJECTIF : Nombre de SUCCÈS voulus par FICHIER RSS
TARGET_SUCCESS_PER_FEED = 2
SEARCH_WINDOW_SIZE = 15

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
    print(f"   [UPLOAD] Envoi de {filename}...")
    for asset in release.get_assets():
        if asset.name == filename:
            try: asset.delete_asset()
            except: pass
            break
    try:
        asset = release.upload_asset(filename)
        return asset.browser_download_url
    except Exception as e:
        print(f"      -> Erreur upload: {e}")
        return None

def attempt_download(entry, ydl_opts, release, fg, current_log_file):
    """Fonction atomique de téléchargement"""
    vid_id = entry['id']
    vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(vid_url, download=True)
        if not info: raise Exception("Info vide")

        mp3_filename = f"{vid_id}.mp3"
        
        # Recherche miniature
        thumb_files = glob.glob(f"{vid_id}.*")
        image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
        jpg_filename = None
        for f in thumb_files:
            if any(f.lower().endswith(ext) for ext in image_extensions):
                jpg_filename = f; break
        
        if not os.path.exists(mp3_filename) or os.path.getsize(mp3_filename) < 10000:
            raise Exception("Fichier invalide")

        mp3_url = upload_asset(release, mp3_filename)
        if not mp3_url: raise Exception("Echec Upload")

        thumb_url = None
        if jpg_filename: thumb_url = upload_asset(release, jpg_filename)

        # RSS
        fe = fg.add_entry()
        fe.id(vid_id)
        title = info.get('title') if info else entry.get('title', vid_id)
        desc = info.get('description') if info else entry.get('description', '')
        fe.title(title)
        fe.description(desc)
        
        upload_date_str = info.get('upload_date') if info else None
        if upload_date_str:
            try: fe.pubDate(datetime.datetime.strptime(upload_date_str, '%Y%m%d').replace(tzinfo=datetime.timezone.utc))
            except: fe.pubDate(datetime.datetime.now(datetime.timezone.utc))
        else: fe.pubDate(datetime.datetime.now(datetime.timezone.utc))

        fe.enclosure(mp3_url, 0, 'audio/mpeg')
        if thumb_url: fe.podcast.itunes_image(thumb_url)

        # Log
        with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
        return True

def run():
    try:
        # 0. Setup
        print("--- Démarrage (Stratégie Cookie Fallback) ---")
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        has_cookies = False
        if cookies_env:
            print("Cookies chargés.")
            with open(COOKIE_FILE, 'w') as f: f.write(cookies_env)
            has_cookies = True
        
        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)

        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # CONFIGURATION YT-DLP ULTIME
        # On utilise 'best' pour prendre n'importe quoi qui ressemble à une vidéo/audio
        # On utilise le client 'android' par défaut qui est le plus robuste
        base_opts = {
            'quiet': False, 
            'ignoreerrors': True,
            'no_warnings': True, 
            'socket_timeout': 60,
            'cachedir': False,
            'format': 'best', # Format le plus permissif possible
            'outtmpl': '%(id)s.%(ext)s', 
            'writethumbnail': True,
            'retries': 10, 'fragment_retries': 10, 
            'skip_unavailable_fragments': False, 
            'abort_on_unavailable_fragment': True,
            'extractor_args': {'youtube': {'player_client': ['android']}}, # Android est souvent le plus stable
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
        }
        if proxy_url: base_opts['proxy'] = proxy_url

        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if not fname: continue
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        for filename, sources in grouped_feeds.items():
            print(f"\n=== Flux RSS : {filename} ===")
            main_config = sources[0]
            custom_title = main_config.get('podcast_name')
            custom_desc = main_config.get('podcast_description')
            custom_image = main_config.get('cover_image')
            custom_author = main_config.get('podcast_author')
            
            current_log_file = f"log_{filename}.txt"
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # SCAN (Avec Cookies si dispo)
            missing_entries = []
            scan_opts = base_opts.copy()
            scan_opts['extract_flat'] = True
            if has_cookies: scan_opts['cookiefile'] = COOKIE_FILE
            
            print("--- Scan ---")
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    url = source.get('url')
                    if not url: continue
                    try:
                        info = ydl_scan.extract_info(url, download=False)
                        if info and 'entries' in info:
                            for entry in info['entries']:
                                if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                    missing_entries.append(entry)
                    except: pass

            print(f"Total Manquants : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # RSS Setup
            fg = FeedGenerator(); fg.load_extension('podcast')
            rss_loaded = False
            if os.path.exists(filename):
                try: fg.parse_file(filename); rss_loaded = True
                except: pass
            
            final_title = custom_title if custom_title else f'Podcast {filename}'
            
            if not rss_loaded:
                fg.title(final_title); fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate'); fg.description('Auto')
            else:
                fg.title(final_title)

            if custom_image: fg.podcast.itunes_image(custom_image); fg.image(url=custom_image, title=final_title, link=f'https://github.com/{REPO_NAME}')
            if custom_author: fg.author({'name': custom_author}); fg.podcast.itunes_author(custom_author)

            # DOWNLOAD LOOP
            success_count = 0
            
            for entry in batch_to_process:
                if success_count >= TARGET_SUCCESS_PER_FEED: break
                vid_id = entry['id']
                print(f"-> Traitement : {entry.get('title', vid_id)}")
                
                # --- TENTATIVE 1 : AVEC COOKIES (Si dispo) ---
                try:
                    print("   [ESSAI 1] Avec Cookies...")
                    current_opts = base_opts.copy()
                    if has_cookies: current_opts['cookiefile'] = COOKIE_FILE
                    
                    if main_config.get('sponsorblock_categories'):
                        current_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

                    attempt_download(entry, current_opts, release, fg, current_log_file)
                    print("   [SUCCÈS] Mode Authentifié.")
                    success_count += 1
                    cleanup_files(vid_id)
                    continue # On passe à la vidéo suivante
                except Exception as e:
                    print(f"   [ECHEC 1] {e}")
                    # Si c'est une erreur fatale (Deleted), on arrête là
                    if "deleted" in str(e).lower() or "account associated" in str(e).lower():
                        print("      -> Vidéo Supprimée. Blacklist.")
                        with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                        cleanup_files(vid_id)
                        continue

                # --- TENTATIVE 2 : SANS COOKIES (Anonyme via Tor) ---
                print("   [ESSAI 2] Mode Anonyme (Sans Cookies)...")
                try:
                    # On retire explicitement le fichier cookie
                    current_opts = base_opts.copy()
                    if 'cookiefile' in current_opts: del current_opts['cookiefile']
                    
                    if main_config.get('sponsorblock_categories'):
                        current_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

                    attempt_download(entry, current_opts, release, fg, current_log_file)
                    print("   [SUCCÈS] Mode Anonyme.")
                    success_count += 1
                except Exception as e:
                    print(f"   [ECHEC TOTAL] {e}")
                    # Si erreur Private ici, on skip simplement
                
                cleanup_files(vid_id)

            fg.rss_file(filename)
            print(f"Sauvegarde XML {filename}")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()


