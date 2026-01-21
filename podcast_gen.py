import os
import json
import datetime
import yt_dlp
import time
import glob
import subprocess
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

def get_tor_ip():
    """Récupère l'IP actuelle vue à travers Tor pour vérification."""
    try:
        # On utilise curl car c'est plus fiable pour tester le SOCKS5 système
        result = subprocess.check_output(
            ["curl", "-s", "--socks5", "127.0.0.1:9050", "https://api.ipify.org"], 
            timeout=10
        ).decode("utf-8").strip()
        return result
    except Exception:
        return "Inconnue (Timeout/Erreur)"

def renew_tor_ip():
    """Force Tor à changer de circuit (nouvelle IP) via le ControlPort."""
    print("   [TOR] --- Tentative de rotation d'IP ---")
    old_ip = get_tor_ip()
    print(f"   [TOR] IP Actuelle : {old_ip}")
    
    try:
        # Connexion au ControlPort
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
            
        print("   [TOR] Signal envoyé. Stabilisation (15s)...")
        time.sleep(15)
        
        new_ip = get_tor_ip()
        print(f"   [TOR] Nouvelle IP : {new_ip}")
        
        if old_ip == new_ip and old_ip != "Inconnue (Timeout/Erreur)":
            print("   [TOR WARN] L'IP n'a pas changé ! (Peut-être peu de noeuds disponibles en BE)")
            
    except Exception as e:
        print(f"   [TOR ERROR] Echec rotation : {e}")
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
        
        # Le proxy URL global (ex: socks5://127.0.0.1:9050)
        global_proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        # GESTION COOKIES
        use_cookies = False
        if cookies_env:
            print("Cookies présents en ENV.")
            with open(COOKIE_FILE, 'w') as f: f.write(cookies_env)
            use_cookies = True
        else:
            print("INFO: Pas de cookies.")

        if not os.path.exists(CONFIG_FILE): 
            print(f"Erreur: {CONFIG_FILE} introuvable.")
            return
        
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # Options de base (Communes)
        base_opts = {
            'quiet': False, 
            'ignoreerrors': True, 
            'no_warnings': True, 
            'socket_timeout': 30,
            'nocheckcertificate': True,
            # On simule un client Android pour éviter les blocs "Browser"
            'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
        }
        
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

            # --- CONFIGURATION PROXY "A LA CARTE" ---
            # Si "force_tor": true est dans le JSON, on active le proxy.
            # Sinon, on reste en connexion directe (plus fiable pour les vidéos non géobloquées)
            feed_needs_tor = main_config.get('force_tor', False)
            
            feed_dl_opts = base_opts.copy()
            
            if feed_needs_tor and global_proxy_url:
                print(f"   [CONFIG] Mode TOR activé pour ce podcast (force_tor=True).")
                feed_dl_opts['proxy'] = global_proxy_url
            else:
                print(f"   [CONFIG] Mode DIRECT (Pas de proxy).")
                # On s'assure qu'aucun proxy n'est défini
                if 'proxy' in feed_dl_opts: del feed_dl_opts['proxy']

            # 1. SCAN (Scan léger)
            missing_entries = []
            auto_title, auto_description = None, None
            
            scan_opts = feed_dl_opts.copy()
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

            # 2. SETUP RSS
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

            # 3. DOWNLOAD
            # On configure les options de téléchargement finales
            dl_opts = feed_dl_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 
                'outtmpl': '%(id)s.%(ext)s', 
                'writethumbnail': True,
                'retries': 3, 
                'fragment_retries': 3, 
                'skip_unavailable_fragments': False,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
            })
            
            if main_config.get('sponsorblock_categories'):
                dl_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

            success_count = 0
            
            for entry in batch_to_process:
                if success_count >= TARGET_SUCCESS_PER_FEED: break
                
                vid_id = entry['id']
                attempts = 0
                max_attempts = 3 if feed_needs_tor else 1 # Retry uniquement utile si on est sous Tor
                
                while attempts < max_attempts:
                    try:
                        if attempts > 0: print(f"   [RETRY] Tentative {attempts+1}...")
                        
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            process_video_download(entry, ydl, release, fg, current_log_file)
                        
                        print("   [SUCCÈS]")
                        success_count += 1
                        cleanup_files(vid_id)
                        break 
                        
                    except Exception as e:
                        err_str = str(e).lower()
                        print(f"   [ERREUR] {e}")
                        cleanup_files(vid_id)
                        
                        # LOGIQUE D'ABANDON OU DE RETRY
                        
                        # Cas 1 : Vidéo Privée -> STOP IMMEDIAT pour cette vidéo
                        if "private video" in err_str:
                            print("      -> Vidéo PRIVÉE détectée. ABANDON DÉFINITIF pour cette exécution.")
                            break # Sort du while, passe à la vidéo suivante dans le 'for'

                        # Cas 2 : Erreur Bot/403 ET on est sous Tor -> Rotation IP + Retry
                        elif feed_needs_tor and ("403" in err_str or "forbidden" in err_str or "bot" in err_str):
                            print("      -> Blocage détecté sous Tor. Rotation d'IP...")
                            renew_tor_ip()
                            attempts += 1
                            
                        # Cas 3 : Autres erreurs (Réseau, etc)
                        else:
                            attempts += 1
                            time.sleep(5)

            fg.rss_file(filename)
            print(f"XML {filename} mis à jour.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
