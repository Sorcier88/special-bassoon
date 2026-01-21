import os
import json
import datetime
import yt_dlp
import time
import glob
from github import Github
from feedgen.feed import FeedGenerator
from stem import Signal
from stem.control import Controller

# --- CONSTANTES ---
REPO_NAME = os.environ['GITHUB_REPOSITORY']
RELEASE_TAG = "audio-storage"
CONFIG_FILE = "playlists.json"
COOKIE_FILE = "cookies.txt"

# OBJECTIF : Nombre de SUCCÈS voulus par FICHIER RSS (Flux)
TARGET_SUCCESS_PER_FEED = 2
SEARCH_WINDOW_SIZE = 20

def renew_tor_ip():
    """Force Tor à changer de circuit (nouvelle IP) via le ControlPort."""
    try:
        print("   [TOR] Demande de nouvelle identité...")
        # Connexion au ControlPort (nécessite CookieAuth configuré dans le YAML)
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        print("   [TOR] Circuit renouvelé. Pause 15s pour stabilisation...")
        time.sleep(15)
    except Exception as e:
        print(f"   [TOR ERROR] Impossible de renouveler l'IP : {e}")
        # On attend quand même un peu si le control port échoue
        time.sleep(10)

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
    print(f"   [UPLOAD] Envoi de {filename} vers GitHub...")
    for asset in release.get_assets():
        if asset.name == filename:
            print(f"      -> Ancienne version trouvée. Suppression...")
            asset.delete_asset()
            break
    try:
        asset = release.upload_asset(filename)
        return asset.browser_download_url
    except Exception as e:
        print(f"      -> Erreur upload {filename}: {e}")
        return None

def process_video_download(entry, ydl, release, fg, current_log_file):
    vid_id = entry['id']
    vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    print(f"-> Traitement : {entry.get('title', vid_id)}")

    # 1. Téléchargement
    # Note: L'exception sera levée ici si 403
    info = ydl.extract_info(vid_url, download=True)
    if not info: raise Exception("Echec silencieux (Info vide)")

    mp3_filename = f"{vid_id}.mp3"
    
    # Recherche miniature
    thumb_files = glob.glob(f"{vid_id}.*")
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    jpg_filename = None
    for f in thumb_files:
        if any(f.lower().endswith(ext) for ext in image_extensions):
            jpg_filename = f; break
    
    if not os.path.exists(mp3_filename) or os.path.getsize(mp3_filename) < 10000:
        raise Exception("Fichier MP3 invalide ou trop petit")

    # 2. Upload
    mp3_url = upload_asset(release, mp3_filename)
    if not mp3_url: raise Exception("Echec Upload GitHub MP3")

    thumb_url = None
    if jpg_filename: 
        thumb_url = upload_asset(release, jpg_filename)
    else:
        print("   [INFO] Pas de miniature spécifique trouvée.")

    # 3. RSS
    fe = fg.add_entry()
    fe.id(vid_id)
    fe.title(info.get('title', entry.get('title', vid_id)))
    fe.description(info.get('description', ''))
    
    upload_date_str = info.get('upload_date')
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
        print("--- Démarrage du script ---")
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        # GESTION COOKIES : On évite de les utiliser sauf si nécessaire
        # car Cookies + Tor = 403 garanti souvent.
        use_cookies = False
        if cookies_env:
            print("Cookies présents en ENV.")
            with open(COOKIE_FILE, 'w') as f: f.write(cookies_env)
            use_cookies = True
        else:
            print("INFO: Pas de cookies.")

        if proxy_url: print(f"Mode TOR activé: {proxy_url}")
        
        if not os.path.exists(CONFIG_FILE): 
            print(f"Erreur: {CONFIG_FILE} introuvable.")
            return
        
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # Options yt-dlp optimisées Anti-Ban
        base_opts = {
            'quiet': False, 
            'ignoreerrors': True, 
            'no_warnings': True, 
            'socket_timeout': 30,
            # IMPORTANT : Impersonate browser requests
            'nocheckcertificate': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }
        
        if proxy_url: base_opts['proxy'] = proxy_url
        
        # On n'active les cookies que si vraiment nécessaire, ou on teste sans d'abord
        # Pour ce fix, on les met si présents, mais voir note plus bas
        if use_cookies and os.path.exists(COOKIE_FILE): 
            base_opts['cookiefile'] = COOKIE_FILE

        # --- REGROUPEMENT PAR FICHIER RSS ---
        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if not fname: continue
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        # --- TRAITEMENT ---
        for filename, sources in grouped_feeds.items():
            print(f"\n=== FLUX : {filename} ===")
            
            main_config = sources[0]
            current_log_file = f"log_{filename}.txt"
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # 1. SCAN (Sans téléchargement lourd)
            missing_entries = []
            auto_title, auto_description = None, None
            
            # Pour le scan, on évite les cookies pour réduire le risque de flag
            scan_opts = base_opts.copy()
            scan_opts['extract_flat'] = True
            
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    try:
                        info = ydl_scan.extract_info(source['url'], download=False)
                        if info:
                            if not auto_title: auto_title = info.get('title')
                            if not auto_description: auto_description = info.get('description')
                            
                            if 'entries' in info:
                                for entry in info['entries']:
                                    if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                        missing_entries.append(entry)
                    except Exception as e:
                        print(f"Scan Error: {e}")

            print(f"Vidéos manquantes : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # 2. SETUP RSS (Code existant simplifié pour focus debug)
            fg = FeedGenerator(); fg.load_extension('podcast')
            rss_loaded = False
            if os.path.exists(filename):
                try: fg.parse_file(filename); rss_loaded = True
                except: pass
            
            final_title = main_config.get('podcast_name') or auto_title or f'Podcast {filename}'
            fg.title(final_title)
            fg.description(main_config.get('podcast_description') or 'Generated Feed')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            
            if main_config.get('cover_image'): 
                fg.podcast.itunes_image(main_config['cover_image'])
                fg.image(url=main_config['cover_image'], title=final_title, link=f'https://github.com/{REPO_NAME}')

            # 3. DOWNLOAD AVEC ROTATION IP
            dl_opts = base_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 
                'outtmpl': '%(id)s.%(ext)s', 
                'writethumbnail': True,
                # Réduction des retries internes car on gère nous-même la rotation IP
                'retries': 3, 
                'fragment_retries': 3, 
                'skip_unavailable_fragments': False,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
            })
            
            if main_config.get('sponsorblock_categories'):
                dl_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

            success_count = 0
            
            # On instancie yt-dlp UNE FOIS par vidéo pour permettre la rotation de config/IP entre temps si besoin
            for entry in batch_to_process:
                if success_count >= TARGET_SUCCESS_PER_FEED: break
                
                vid_id = entry['id']
                attempts = 0
                max_attempts = 3 # Tentatives globales avec changement d'IP
                
                while attempts < max_attempts:
                    try:
                        print(f"DL Tentative {attempts+1}/{max_attempts} pour {vid_id}...")
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            process_video_download(entry, ydl, release, fg, current_log_file)
                        
                        print("   [SUCCÈS]")
                        success_count += 1
                        cleanup_files(vid_id)
                        break # Sortie du While
                        
                    except Exception as e:
                        err_str = str(e).lower()
                        print(f"   [ERREUR] {e}")
                        
                        cleanup_files(vid_id)
                        
                        # Gestion intelligente des erreurs
                        if "403" in err_str or "forbidden" in err_str:
                            print("      -> ERREUR 403 DÉTECTÉE. L'IP EST PROBABLEMENT BANNIE.")
                            renew_tor_ip() # On change d'IP
                            attempts += 1
                        elif "sign in" in err_str or "private" in err_str:
                            print("      -> Vidéo privée ou login requis. Skip.")
                            break # On abandonne cette vidéo
                        else:
                            # Autre erreur (réseau, etc), on réessaie une fois
                            time.sleep(5)
                            attempts += 1

            fg.rss_file(filename)
            print(f"XML {filename} mis à jour.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
