import os
import json
import datetime
import yt_dlp
import time
import glob
import random
import shutil
import subprocess
from github import Github
from feedgen.feed import FeedGenerator
from stem import Signal
from stem.control import Controller

# --- CONSTANTES DE BASE ---
REPO_NAME = os.environ.get('GITHUB_REPOSITORY', 'local-test')
RELEASE_TAG = "audio-storage"
CONFIG_FILE = "playlists.json"
COOKIE_FILE = "cookies.txt"
LOG_DIR = "logs"  # Nouveau dossier pour les logs

# CONFIGURATION STANDARD (Maintenance Quotidienne)
DEFAULT_TARGET_SUCCESS = 3
DEFAULT_SEARCH_WINDOW = 30

# CONFIGURATION "REFILL" (Si le flux est vide/maigre)
REFILL_TARGET_SUCCESS = 5   # On en télécharge 10 d'un coup
REFILL_SEARCH_WINDOW = 50   # On regarde 100 vidéos en arrière

def get_tor_info():
    """Récupère l'IP actuelle et le PAYS via Tor."""
    try:
        result = subprocess.check_output(
            ["curl", "-s", "--socks5", "127.0.0.1:9050", "http://ip-api.com/json"], 
            timeout=20
        ).decode("utf-8").strip()
        data = json.loads(result)
        return f"{data.get('query')} [{data.get('countryCode')}]"
    except Exception:
        return "IP Inconnue"

def get_controller():
    try:
        controller = Controller.from_port(port=9051)
        controller.authenticate() 
        return controller
    except Exception as e:
        print(f"   [TOR FATAL] Impossible de se connecter au port 9051 : {e}")
        return None

def configure_tor_nodes(country_codes=None):
    controller = get_controller()
    if not controller: return

    try:
        if country_codes:
            print(f"   [TOR CONFIG] Restriction géographique -> {country_codes}")
            nodes_formatted = ",".join([f"{{{c.strip()}}}" for c in country_codes.split(',')])
            controller.set_conf('ExitNodes', nodes_formatted)
            controller.set_conf('StrictNodes', '1')
        else:
            print(f"   [TOR CONFIG] Reset géographique (Monde entier)")
            controller.reset_conf('ExitNodes')
            controller.reset_conf('StrictNodes')

        controller.signal(Signal.NEWNYM)
        time.sleep(10)
        controller.close()
    except Exception as e:
        print(f"   [TOR ERROR] Erreur config Tor : {e}")
        if controller: controller.close()

def renew_tor_ip():
    print("   [TOR] --- Rotation d'IP ---")
    controller = get_controller()
    if not controller: return
    try:
        controller.signal(Signal.NEWNYM)
        time.sleep(10)
        controller.close()
    except Exception:
        if controller: controller.close()

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

    info = ydl.extract_info(vid_url, download=True)
    mp3_filename = f"{vid_id}.mp3"
    
    thumb_files = glob.glob(f"{vid_id}.*")
    image_extensions = ['.jpg', '.jpeg', '.png', '.webp']
    jpg_filename = None
    for f in thumb_files:
        if any(f.lower().endswith(ext) for ext in image_extensions):
            jpg_filename = f; break
    
    if not os.path.exists(mp3_filename) or os.path.getsize(mp3_filename) < 10000:
        raise Exception("Fichier MP3 invalide ou trop petit")

    mp3_url = upload_asset(release, mp3_filename)
    if not mp3_url: raise Exception("Echec Upload GitHub MP3")

    thumb_url = None
    if jpg_filename: thumb_url = upload_asset(release, jpg_filename)

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

    # Ecriture dans le dossier logs/
    with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
    return True

def run():
    try:
        print("--- Démarrage du script (V8 - CLEAN LOGS & SMART REFILL) ---")
        global_proxy_url = os.environ.get('YOUTUBE_PROXY')
        
        # Création automatique du dossier logs s'il n'existe pas
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
            print(f"Dossier '{LOG_DIR}' créé.")

        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        base_opts = {
            'quiet': False, 
            'no_warnings': True, 
            'socket_timeout': 30,
            'nocheckcertificate': True,
            'proxy': global_proxy_url,
            'http_headers': {
                'Cookie': 'CONSENT=YES+cb.20210328-17-p0.en+FX+419; SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLCPpgY'
            },
            'sleep_interval': 15,
            'max_sleep_interval': 30,
        }

        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        for filename, sources in grouped_feeds.items():
            print(f"\n==========================================")
            print(f"FLUX : {filename}")
            print(f"==========================================")
            time.sleep(5)
            
            main_config = sources[0]
            # Modification du chemin des logs : logs/log_nomdufichier.txt
            current_log_file = os.path.join(LOG_DIR, f"log_{filename}.txt")
            
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # --- GESTION TOR ---
            tor_exit_nodes = main_config.get('tor_exit_nodes')
            if tor_exit_nodes: configure_tor_nodes(tor_exit_nodes)
            else: configure_tor_nodes(None)
            print(f"   [DEBUG] IP utilisée : {get_tor_info()}")

            # 1. RSS SETUP & DETECTION MODE REFILL
            fg = FeedGenerator(); fg.load_extension('podcast')
            existing_entries_count = 0
            
            if os.path.exists(filename):
                try:
                    fg.parse_file(filename)
                    existing_entries_count = len(fg.entry())
                    print(f"   [RSS OK] {existing_entries_count} épisodes chargés depuis l'historique.")
                except Exception as e:
                    print(f"   [RSS ERROR] CRITIQUE : Impossible de lire {filename} : {e}")
                    print("   -> Backup du fichier corrompu.")
                    shutil.copy(filename, filename + ".corrupted")
            
            # --- LOGIQUE SMART REFILL ---
            if existing_entries_count < 5:
                print("   [AUTO-REFILL] Le flux est trop maigre (< 5 épisodes).")
                print(f"   -> ACTIVATION MODE REMPLISSAGE (Cible: {REFILL_TARGET_SUCCESS} nouveaux, Scan: {REFILL_SEARCH_WINDOW})")
                current_target = REFILL_TARGET_SUCCESS
                current_window = REFILL_SEARCH_WINDOW
            else:
                print("   [MAINTENANCE] Flux sain.")
                current_target = DEFAULT_TARGET_SUCCESS
                current_window = DEFAULT_SEARCH_WINDOW

            # 2. SCAN
            missing_entries = []
            auto_title = None
            auto_description = None
            
            scan_opts = base_opts.copy()
            scan_opts['extract_flat'] = True
            scan_opts['ignoreerrors'] = True
            scan_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}
            
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    try:
                        info = ydl_scan.extract_info(source['url'], download=False)
                        if info:
                            if not auto_title and info.get('title'): auto_title = info.get('title')
                            if not auto_description and info.get('description'): auto_description = info.get('description')
                            
                            if 'entries' in info:
                                for entry in info['entries']:
                                    if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                        missing_entries.append(entry)
                    except Exception as e: print(f"Scan Warning: {e}")

            print(f"Vidéos manquantes trouvées : {len(missing_entries)}")
            
            batch_to_process = missing_entries[:current_window]
            print(f"Vidéos retenues pour traitement : {len(batch_to_process)}")

            # 2b. METADONNEES
            final_title = main_config.get('podcast_name') or auto_title or f'Podcast {filename}'
            fg.title(final_title)
            final_desc = main_config.get('podcast_description') or auto_description or 'Generated Feed'
            fg.description(final_desc)
            
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            if main_config.get('cover_image'): 
                fg.podcast.itunes_image(main_config['cover_image'])
                fg.image(url=main_config['cover_image'], title=final_title, link=f'https://github.com/{REPO_NAME}')
            if main_config.get('podcast_author'):
                 fg.author({'name': main_config.get('podcast_author')})
                 fg.podcast.itunes_author(main_config.get('podcast_author'))

            # 3. DOWNLOAD
            dl_opts_base = base_opts.copy()
            dl_opts_base.update({
                'format': 'bestaudio/best', 
                'outtmpl': '%(id)s.%(ext)s', 
                'writethumbnail': True,
                'ignoreerrors': False,
                'retries': 5,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
            })
            
            if main_config.get('sponsorblock_categories'):
                dl_opts_base['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

            success_count = 0
            changes_made = False
            
            for entry in batch_to_process:
                if success_count >= current_target: 
                    print(f"Objectif atteint ({current_target} nouveaux épisodes). Arrêt.")
                    break
                
                vid_id = entry['id']
                attempts = 0
                max_attempts = 2
                current_client = ['android', 'ios']
                
                while attempts < max_attempts:
                    dl_opts = dl_opts_base.copy()
                    dl_opts['extractor_args'] = {'youtube': {'player_client': current_client}}
                    try:
                        print(f"   [DL] {vid_id}...")
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            if process_video_download(entry, ydl, release, fg, current_log_file):
                                changes_made = True
                        print("   [SUCCÈS]")
                        success_count += 1
                        cleanup_files(vid_id)
                        time.sleep(random.randint(10, 20))
                        break 
                    except Exception as e:
                        err_str = str(e).lower()
                        print(f"   [ERREUR] {str(e)[:100]}...")
                        cleanup_files(vid_id)
                        if "private" in err_str or "sign in" in err_str:
                            if attempts == 0:
                                renew_tor_ip()
                                current_client = ['web']
                                attempts += 1
                                continue
                            else: break
                        elif "country" in err_str or "403" in err_str or "bot" in err_str:
                            renew_tor_ip()
                            time.sleep(15)
                            attempts += 1
                        else:
                            time.sleep(5)
                            attempts += 1

            # 4. SAUVEGARDE
            current_entries = len(fg.entry())
            print(f"   [INFO] Total épisodes : {current_entries}")
            
            if changes_made or not os.path.exists(filename) or (auto_description and not main_config.get('podcast_description')):
                if existing_entries_count > 5 and current_entries < 5:
                    print("   [SECURITY ALERT] CHUTE DRASTIQUE D'EPISODES !")
                    fg.rss_file(filename + ".DANGER_CHECK")
                else:
                    if os.path.exists(filename):
                        shutil.copy(filename, filename + ".bak")
                    fg.rss_file(filename)
                    print(f"XML {filename} mis à jour avec succès.")
            else:
                print("Aucun changement majeur, pas d'écriture XML.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
