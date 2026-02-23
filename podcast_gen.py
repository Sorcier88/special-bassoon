import os
import json
import datetime
import yt_dlp
import time
import glob
import random
import shutil
import subprocess
import requests
import xml.etree.ElementTree as ET
from github import Github
from feedgen.feed import FeedGenerator
from stem import Signal
from stem.control import Controller

# --- CONSTANTES ---
REPO_NAME = os.environ.get('GITHUB_REPOSITORY', 'local-test')
RELEASE_TAG = "audio-storage"
CONFIG_FILE = "playlists.json"
COOKIE_FILE = "cookies.txt"
LOG_DIR = "logs"

# CHRONOMETRE ANTI-TIMEOUT
MAX_RUNTIME_SECONDS = (5 * 3600) + (15 * 60)
script_start_time = time.time()

# CONFIGURATION
DEFAULT_TARGET_SUCCESS = 3
DEFAULT_SEARCH_WINDOW = 30
REFILL_TARGET_SUCCESS = 10
REFILL_SEARCH_WINDOW = 50

def check_timeout():
    elapsed = time.time() - script_start_time
    if elapsed > MAX_RUNTIME_SECONDS:
        print(f"\n[!!!] TEMPS LIMITE ATTEINT ({int(elapsed/60)} minutes).")
        print("[!!!] Arrêt d'urgence pour sauvegarde.")
        return True
    return False

def get_tor_info():
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

# --- NOUVELLE FONCTION UPLOAD AVEC GITHUB CLI (ULTRA ROBUSTE) ---
def upload_asset(release, filename):
    if not os.path.exists(filename): return None
    print(f"   [UPLOAD] Préparation envoi de {filename} via GitHub CLI...")
    
    # URL statique prédictible de GitHub Releases
    expected_url = f"https://github.com/{REPO_NAME}/releases/download/{RELEASE_TAG}/{filename}"
            
    for attempt in range(1, 4):
        try:
            print(f"      -> Tentative d'upload {attempt}/3...")
            # Appel natif à l'outil 'gh' préinstallé sur le runner Ubuntu.
            # L'option --clobber écrase automatiquement le fichier s'il existe (évite le nettoyage manuel).
            cmd = ["gh", "release", "upload", RELEASE_TAG, filename, "--clobber"]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            print("      -> Upload réussi avec succès !")
            return expected_url
            
        except subprocess.CalledProcessError as e:
            # Capturer l'erreur exacte du CLI
            print(f"      [ERREUR UPLOAD CLI] {e.stderr.strip()}")
            if attempt < 3:
                wait_time = attempt * 10
                print(f"      -> Pause de {wait_time}s avant retry...")
                time.sleep(wait_time)
            else:
                print("      -> ECHEC FATAL après 3 tentatives.")
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

    with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
    return True

def recover_entries_from_xml(filename, fg):
    count = 0
    if not os.path.exists(filename): 
        print(f"   [XML] Fichier {filename} inexistant. Démarrage de zéro.")
        return 0
    try:
        tree = ET.parse(filename)
        root = tree.getroot()
        channel = root.find('channel')
        if channel is None: return 0
        for item in channel.findall('item'):
            fe = fg.add_entry()
            t = item.find('title'); 
            if t is not None: fe.title(t.text)
            d = item.find('description'); 
            if d is not None: fe.description(d.text)
            g = item.find('guid'); 
            if g is not None: fe.id(g.text)
            p = item.find('pubDate'); 
            if p is not None: fe.pubDate(p.text)
            enc = item.find('enclosure')
            if enc is not None: fe.enclosure(enc.get('url'), 0, enc.get('type'))
            itunes_ns = 'http://www.itunes.com/dtds/podcast-1.0.dtd'
            img = item.find(f'{{{itunes_ns}}}image')
            if img is not None: fe.podcast.itunes_image(img.get('href'))
            count += 1
        print(f"   [XML RESTORE] {count} anciens épisodes restaurés depuis {filename}.")
        return count
    except Exception as e:
        print(f"   [XML READ ERROR] Impossible de lire {filename} : {e}")
        return 0

def run():
    try:
        print("--- Démarrage du script (VERSION V14 - ULTRA ROBUST UPLOAD) ---")
        
        # Nettoyage profond des variables d'environnement proxy pour ne pas perturber les uploads
        for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
            if k in os.environ: del os.environ[k]
            
        global_proxy_url = "socks5://127.0.0.1:9050" 
        
        if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
        if not os.path.exists(CONFIG_FILE): return
        
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            # On utilise PyGithub juste pour s'assurer que la Release "audio-storage" existe au départ
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur d'initialisation GitHub: {e}"); return

        base_opts = {
            'quiet': False, 
            'no_warnings': True, 
            'socket_timeout': 30,
            'nocheckcertificate': True,
            'proxy': global_proxy_url,
            'http_headers': {'Cookie': 'CONSENT=YES+cb.20210328-17-p0.en+FX+419; SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLCPpgY'},
            'sleep_interval': 10, 'max_sleep_interval': 25,
        }

        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        for filename, sources in grouped_feeds.items():
            
            if check_timeout():
                break

            print(f"\n==========================================")
            print(f"=== FLUX : {filename} ===")
            print(f"==========================================")
            time.sleep(2)
            
            main_config = sources[0]
            current_log_file = os.path.join(LOG_DIR, f"log_{filename}.txt")
            
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            tor_exit_nodes = main_config.get('tor_exit_nodes')
            if tor_exit_nodes: configure_tor_nodes(tor_exit_nodes)
            else: configure_tor_nodes(None)
            print(f"   [DEBUG] IP: {get_tor_info()}")

            fg = FeedGenerator(); fg.load_extension('podcast')
            
            existing_entries_count = 0 
            if os.path.exists(filename):
                existing_entries_count = recover_entries_from_xml(filename, fg)
            
            if existing_entries_count < 5:
                print(f"   [AUTO-REFILL] Seulement {existing_entries_count} épisodes. Activation Remplissage.")
                current_target = REFILL_TARGET_SUCCESS
                current_window = REFILL_SEARCH_WINDOW
            else:
                print(f"   [MAINTENANCE] Flux sain avec {existing_entries_count} épisodes.")
                current_target = DEFAULT_TARGET_SUCCESS
                current_window = DEFAULT_SEARCH_WINDOW

            missing_entries = []
            auto_title, auto_description = None, None
            scan_opts = base_opts.copy(); scan_opts.update({'extract_flat': True, 'ignoreerrors': True})
            scan_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}
            
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
                    except Exception as e: print(f"Scan Warn: {e}")

            batch_to_process = missing_entries[:current_window]
            print(f"   Vidéos à traiter : {len(batch_to_process)}")

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

            dl_opts = base_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 'outtmpl': '%(id)s.%(ext)s', 'writethumbnail': True,
                'ignoreerrors': False, 'retries': 5,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}, {'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}, {'key': 'EmbedThumbnail'}, {'key': 'FFmpegMetadata', 'add_metadata': True}],
            })
            if main_config.get('sponsorblock_categories'): dl_opts['sponsorblock_remove'] = main_config.get('sponsorblock_categories')

            success_count = 0
            changes_made = False
            for entry in batch_to_process:
                if check_timeout():
                    break
                
                if success_count >= current_target: break
                
                vid_id = entry['id']
                attempts = 0
                current_client = ['android', 'ios']
                while attempts < 2:
                    dl_opts['extractor_args'] = {'youtube': {'player_client': current_client}}
                    try:
                        print(f"   [DL] {vid_id}...")
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            if process_video_download(entry, ydl, release, fg, current_log_file): changes_made = True
                        print("   [SUCCÈS]")
                        success_count += 1
                        cleanup_files(vid_id)
                        time.sleep(random.randint(5, 15))
                        break 
                    except Exception as e:
                        err_str = str(e).lower()
                        print(f"   [ERR] {str(e)[:50]}...")
                        cleanup_files(vid_id)
                        if "private" in err_str or "sign in" in err_str:
                            if attempts == 0:
                                renew_tor_ip(); current_client = ['web']; attempts += 1; continue
                            else: break
                        elif "country" in err_str or "403" in err_str or "bot" in err_str:
                            renew_tor_ip(); time.sleep(10); attempts += 1
                        else: time.sleep(5); attempts += 1

            current_entries = len(fg.entry())
            print(f"   [INFO] Total dans ce flux : {current_entries}")
            
            if changes_made or not os.path.exists(filename) or (auto_description and not main_config.get('podcast_description')):
                if existing_entries_count > 5 and current_entries < 5:
                    print("   [ALERT] Chute brutale ! Backup DANGER_CHECK.")
                    fg.rss_file(filename + ".DANGER_CHECK")
                else:
                    if os.path.exists(filename): shutil.copy(filename, filename + ".bak")
                    fg.rss_file(filename)
                    print(f"XML mis à jour avec succès.")
            else: print("Pas de changement majeur pour ce flux.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()