import os
import json
import datetime
import yt_dlp
from yt_dlp.utils import DownloadError
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
    """Récupère l'IP actuelle et le PAYS via Tor (debug)."""
    try:
        # Utilisation de ip-api via HTTP (tunnelé Tor) pour avoir le pays
        result = subprocess.check_output(
            ["curl", "-s", "--socks5", "127.0.0.1:9050", "http://ip-api.com/json"], 
            timeout=15
        ).decode("utf-8").strip()
        data = json.loads(result)
        return f"{data.get('query')} [{data.get('countryCode')}]"
    except Exception:
        return "IP Inconnue (Erreur/Timeout)"

def set_tor_exit_nodes(country_codes):
    """
    Configure dynamiquement les noeuds de sortie Tor.
    country_codes : str (ex: "BE" ou "BE,FR,CH")
    """
    if not country_codes: return
    print(f"   [TOR CONFIG] Modification des noeuds de sortie -> {country_codes}")
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            # Format Tor : {BE},{FR}
            nodes_formatted = ",".join([f"{{{c.strip()}}}" for c in country_codes.split(',')])
            controller.set_conf('ExitNodes', nodes_formatted)
            # On force le changement de circuit immédiatement
            controller.signal(Signal.NEWNYM)
            print("   [TOR CONFIG] Application réussie. Stabilisation (10s)...")
            time.sleep(10)
    except Exception as e:
        print(f"   [TOR ERROR] Impossible de changer la config : {e}")

def renew_tor_ip():
    """Force Tor à changer de circuit."""
    print("   [TOR] --- Rotation d'IP demandée ---")
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        print("   [TOR] Signal envoyé. Stabilisation (10s)...")
        time.sleep(10)
        print(f"   [TOR] Nouvelle identité : {get_tor_ip()}")
    except Exception as e:
        print(f"   [TOR ERROR] Echec rotation : {e}")
        time.sleep(5)

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
    # Note: L'option ignoreerrors=False dans dl_opts est CRUCIALE ici
    # pour que yt-dlp lève une vraie exception (PrivateVideo, etc)
    info = ydl.extract_info(vid_url, download=True)
    
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
        print("--- Démarrage du script (Version Senior Fix) ---")
        cookies_env = os.environ.get('YOUTUBE_COOKIES')
        global_proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        use_cookies = False
        if cookies_env:
            print("Cookies présents (Attention aux conflits Tor).")
            with open(COOKIE_FILE, 'w') as f: f.write(cookies_env)
            use_cookies = True

        if not os.path.exists(CONFIG_FILE): 
            print(f"Erreur: {CONFIG_FILE} introuvable.")
            return
        
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # Options de base
        base_opts = {
            'quiet': False, 
            'no_warnings': True, 
            'socket_timeout': 30,
            'nocheckcertificate': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
        }
        if use_cookies and os.path.exists(COOKIE_FILE): 
            base_opts['cookiefile'] = COOKIE_FILE

        # Regroupement
        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if not fname: continue
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        # --- TRAITEMENT PAR FLUX ---
        for filename, sources in grouped_feeds.items():
            print(f"\n=== FLUX : {filename} ===")
            main_config = sources[0]
            current_log_file = f"log_{filename}.txt"
            
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # --- CONFIGURATION RESEAU & PAYS ---
            feed_needs_tor = main_config.get('force_tor', False)
            tor_exit_nodes = main_config.get('tor_exit_nodes') # Ex: "BE,FR"
            
            feed_dl_opts = base_opts.copy()
            
            if feed_needs_tor and global_proxy_url:
                print(f"   [CONFIG] Mode TOR activé.")
                feed_dl_opts['proxy'] = global_proxy_url
                
                # RECONFIGURATION DYNAMIQUE DES PAYS TOR
                if tor_exit_nodes:
                    set_tor_exit_nodes(tor_exit_nodes)
                    print(f"   [DEBUG] IP actuelle après config : {get_tor_ip()}")
                else:
                    # Reset vers Belgique par défaut si non spécifié mais Tor requis
                    # set_tor_exit_nodes("BE") 
                    pass 
            else:
                print(f"   [CONFIG] Mode DIRECT.")
                if 'proxy' in feed_dl_opts: del feed_dl_opts['proxy']

            # 1. SCAN (Léger)
            missing_entries = []
            auto_title = None
            
            scan_opts = feed_dl_opts.copy()
            scan_opts['extract_flat'] = True
            scan_opts['ignoreerrors'] = True # On ignore les erreurs au scan
            
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    try:
                        info = ydl_scan.extract_info(source['url'], download=False)
                        if info:
                            if not auto_title: auto_title = info.get('title')
                            if 'entries' in info:
                                for entry in info['entries']:
                                    if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                        missing_entries.append(entry)
                    except Exception as e:
                        print(f"Scan Warning: {e}")

            print(f"Vidéos manquantes : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # 2. RSS Setup (Simplifié)
            fg = FeedGenerator(); fg.load_extension('podcast')
            if os.path.exists(filename): 
                try: fg.parse_file(filename)
                except: pass
            
            final_title = main_config.get('podcast_name') or auto_title or f'Podcast {filename}'
            fg.title(final_title)
            fg.description(main_config.get('podcast_description') or 'Generated Feed')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            if main_config.get('cover_image'): 
                fg.podcast.itunes_image(main_config['cover_image'])
                fg.image(url=main_config['cover_image'], title=final_title, link=f'https://github.com/{REPO_NAME}')

            # 3. DOWNLOAD
            dl_opts = feed_dl_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 
                'outtmpl': '%(id)s.%(ext)s', 
                'writethumbnail': True,
                'ignoreerrors': False, # <--- IMPORTANT: On veut les VRAIES erreurs maintenant
                'retries': 3,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
            })
            
            if main_config.get('sponsorblock_categories'):
                dl_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

            success_count = 0
            for entry in batch_to_process:
                if success_count >= TARGET_SUCCESS_PER_FEED: break
                
                vid_id = entry['id']
                attempts = 0
                max_attempts = 3 if feed_needs_tor else 1
                
                while attempts < max_attempts:
                    try:
                        if attempts > 0: print(f"   [RETRY] Tentative {attempts+1}...")
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            process_video_download(entry, ydl, release, fg, current_log_file)
                        
                        print("   [SUCCÈS]")
                        success_count += 1
                        cleanup_files(vid_id)
                        break 
                        
                    except Exception as e: # On capture TOUT ici, y compris DownloadError
                        # On nettoie le message d'erreur pour l'analyse
                        err_str = str(e).lower()
                        # Si c'est une DownloadError, le message est explicite
                        
                        print(f"   [ERREUR ANALYSE] {str(e)[:150]}...") # Log court
                        cleanup_files(vid_id)
                        
                        # --- ANALYSE INTELLIGENTE DES ERREURS ---
                        
                        # 1. VIDEO PRIVÉE ou SUPPRIMÉE -> STOP
                        if "private video" in err_str or "this video has been removed" in err_str:
                            print("      -> Vidéo INACCESSIBLE (Privée/Supprimée). On passe à la suivante.")
                            # On ajoute à l'historique pour ne plus la retenter demain
                            # (Optionnel : décommenter la ligne suivante si tu veux ignorer définitivement)
                            # with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
                            break # Sort du While (Retry), passe à la video suivante
                        
                        # 2. GEO-BLOCAGE (Erreur pays)
                        elif "not made this video available in your country" in err_str:
                            print("      -> ERREUR PAYS détectée.")
                            if feed_needs_tor:
                                print("      -> Tentative de changement d'IP Tor...")
                                renew_tor_ip()
                                attempts += 1
                            else:
                                print("      -> Pas de Tor activé. Impossible de contourner.")
                                break

                        # 3. BLOCAGE BOT / 403
                        elif feed_needs_tor and ("403" in err_str or "forbidden" in err_str or "bot" in err_str or "sign in" in err_str):
                            print("      -> Blocage Anti-Bot détecté. Rotation IP...")
                            renew_tor_ip()
                            attempts += 1
                        
                        # 4. AUTRE
                        else:
                            print("      -> Erreur générique. Retry simple.")
                            time.sleep(5)
                            attempts += 1

            fg.rss_file(filename)
            print(f"XML {filename} mis à jour.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
