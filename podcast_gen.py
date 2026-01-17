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
TARGET_SUCCESS_PER_FEED = 3
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

def process_video_download(entry, release, fg, current_log_file, base_opts):
    vid_id = entry['id']
    vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    print(f"-> Traitement : {entry.get('title', vid_id)}")

    # LISTE DES STRATÉGIES (Clients à tester dans l'ordre)
    strategies = [
        {'name': 'Android', 'client': 'android', 'format': 'bestaudio/best'},
        {'name': 'iOS', 'client': 'ios', 'format': 'best'}, # Format 'best' pour iOS car souvent pas de 'bestaudio' séparé
        {'name': 'Web', 'client': 'web', 'format': 'bestaudio/best'},
        {'name': 'TV', 'client': 'tv', 'format': 'best'} # Dernier recours
    ]

    download_success = False
    mp3_filename = f"{vid_id}.mp3"
    info = None

    # BOUCLE SUR LES STRATÉGIES
    for strat in strategies:
        print(f"   [ESSAI] Client : {strat['name']}...")
        
        # Config spécifique pour ce client
        current_opts = base_opts.copy()
        current_opts.update({
            'format': strat['format'],
            'outtmpl': '%(id)s.%(ext)s',
            'writethumbnail': True,
            'extractor_args': {'youtube': {'player_client': [strat['client']]}},
            'retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': False,
            'abort_on_unavailable_fragment': True,
            'postprocessors': [
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata', 'add_metadata': True}
            ],
        })

        try:
            with yt_dlp.YoutubeDL(current_opts) as ydl:
                info = ydl.extract_info(vid_url, download=True)
            
            if os.path.exists(mp3_filename) and os.path.getsize(mp3_filename) > 10000:
                print(f"   [SUCCÈS] Téléchargement réussi avec {strat['name']}!")
                download_success = True
                break # On sort de la boucle de stratégies car on a réussi
            else:
                print(f"   [ECHEC] Fichier non créé avec {strat['name']}.")
                cleanup_files(vid_id)

        except Exception as e:
            err = str(e).lower()
            print(f"   [ERREUR {strat['name']}] {e}")
            # Si c'est une erreur fatale (Supprimée/Privée), inutile de tester les autres clients
            if "deleted" in err or "account associated with this video has been terminated" in err:
                raise Exception("DELETED") # On remonte l'erreur pour blacklist
            if "private video" in err:
                raise Exception("PRIVATE") # On remonte l'erreur pour skip
            
            # Si c'est "Format not available" ou "403" ou "Sign in", on continue à la stratégie suivante
            time.sleep(5) # Petite pause avant de changer de client
            cleanup_files(vid_id)
            continue

    if not download_success:
        raise Exception("Echec avec TOUS les clients (Android, iOS, Web, TV).")

    # 2. Upload
    mp3_url = upload_asset(release, mp3_filename)
    if not mp3_url: raise Exception("Echec Upload GitHub MP3")

    # Recherche miniature
    thumb_files = glob.glob(f"{vid_id}.*")
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    jpg_filename = None
    for f in thumb_files:
        if any(f.lower().endswith(ext) for ext in image_extensions):
            jpg_filename = f; break
            
    thumb_url = None
    if jpg_filename: thumb_url = upload_asset(release, jpg_filename)

    # 3. RSS
    fe = fg.add_entry()
    fe.id(vid_id)
    # On utilise info si dispo, sinon entry
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

    # Log succès
    with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
    return True

def run():
    try:
        # 0. Setup
        print("--- Démarrage du script (Stratégie Caméléon) ---")
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        if cookies_env:
            print("Cookies chargés.")
            with open(COOKIE_FILE, 'w') as f: f.write(cookies_env)
        
        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)

        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # Options de base (Communes)
        base_opts = {
            'quiet': False, 
            'ignoreerrors': True, # Important pour gérer nous-mêmes les erreurs
            'no_warnings': True, 
            'socket_timeout': 60,
            'cachedir': False, # Anti-403
        }
        if proxy_url: base_opts['proxy'] = proxy_url
        if os.path.exists(COOKIE_FILE): base_opts['cookiefile'] = COOKIE_FILE

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
            sb_categories = main_config.get('sponsorblock_categories')
            
            if sb_categories:
                print(f"SponsorBlock activé : {sb_categories}")
                base_opts['sponsorblock_remove'] = sb_categories
            
            current_log_file = f"log_{filename}.txt"
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # SCAN (On utilise le client 'android' pour le scan car il voit souvent plus de choses)
            missing_entries = []
            auto_description = None
            auto_title = None

            print("--- Scan des sources ---")
            scan_opts = base_opts.copy()
            scan_opts['extract_flat'] = True
            scan_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
            
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    url = source.get('url')
                    if not url: continue
                    try:
                        info = ydl_scan.extract_info(url, download=False)
                        if info:
                            if not auto_title: auto_title = info.get('title')
                            if not auto_description: auto_description = info.get('description')
                            if 'entries' in info:
                                for entry in info['entries']:
                                    if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                        missing_entries.append(entry)
                    except Exception as e: print(f"Erreur scan: {e}")

            print(f"Total Manquants : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # RSS Setup
            fg = FeedGenerator(); fg.load_extension('podcast')
            rss_loaded = False
            if os.path.exists(filename):
                try: fg.parse_file(filename); rss_loaded = True
                except: pass
            
            final_title = custom_title if custom_title else (auto_title if auto_title else f'Podcast {filename}')
            final_desc = custom_desc if custom_desc else (auto_description if auto_description else 'Description')
            
            if not rss_loaded:
                fg.title(final_title); fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate'); fg.description(final_desc)
            else:
                fg.title(final_title); fg.description(final_desc)

            if custom_image: fg.podcast.itunes_image(custom_image); fg.image(url=custom_image, title=final_title, link=f'https://github.com/{REPO_NAME}')
            if custom_author: fg.author({'name': custom_author}); fg.podcast.itunes_author(custom_author)

            # DOWNLOAD & RETRY LOGIC
            success_count = 0
            retry_queue = []

            print(f"--- PHASE DOWNLOAD ---")
            # Pour la boucle principale, on passe les bons arguments
            for entry in batch_to_process:
                if success_count >= TARGET_SUCCESS_PER_FEED: break
                vid_id = entry['id']
                try:
                    process_video_download(entry, release, fg, current_log_file, base_opts)
                    print("   [GLOBAL SUCCÈS]")
                    success_count += 1
                    cleanup_files(vid_id)
                except Exception as e:
                    err_str = str(e)
                    print(f"   [GLOBAL ECHEC] {e}")
                    if "DELETED" in err_str:
                        print("      -> Vidéo Supprimée. Blacklist.")
                        with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                    elif "PRIVATE" in err_str:
                        print("      -> Vidéo Privée. Skip.")
                    else:
                        print("      -> Echec technique total. Ajout Retry Queue.")
                        retry_queue.append(entry)
                    cleanup_files(vid_id)

            if retry_queue and success_count < TARGET_SUCCESS_PER_FEED:
                print(f"\n--- PHASE RETRY ({len(retry_queue)} vidéos) ---")
                print("Pause de 60s pour changement IP Tor...")
                time.sleep(60)
                
                for entry in retry_queue:
                    if success_count >= TARGET_SUCCESS_PER_FEED: break
                    vid_id = entry['id']
                    print(f"Retry -> {vid_id}")
                    try:
                        process_video_download(entry, release, fg, current_log_file, base_opts)
                        print("   [GLOBAL SUCCÈS RETRY]")
                        success_count += 1
                    except: pass
                    cleanup_files(vid_id)

            fg.rss_file(filename)
            print(f"Sauvegarde XML {filename}")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()


