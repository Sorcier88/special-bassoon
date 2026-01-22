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

def get_tor_info():
    """Récupère l'IP actuelle et le PAYS via Tor."""
    try:
        # Utilisation de ip-api via HTTP (tunnelé Tor)
        result = subprocess.check_output(
            ["curl", "-s", "--socks5", "127.0.0.1:9050", "http://ip-api.com/json"], 
            timeout=20
        ).decode("utf-8").strip()
        data = json.loads(result)
        return f"{data.get('query')} [{data.get('countryCode')}]"
    except Exception:
        return "IP Inconnue"

def configure_tor_nodes(country_codes=None):
    """
    Configure les noeuds de sortie.
    Si country_codes est None -> Reset vers "N'importe quel pays".
    Si country_codes est "BE,FR" -> Force ces pays.
    """
    try:
        # Connexion sans auth (configuré dans le YAML avec CookieAuthentication 0)
        with Controller.from_port(port=9051) as controller:
            controller.authenticate() # Pas de mot de passe nécessaire
            
            if country_codes:
                print(f"   [TOR CONFIG] Restriction géographique -> {country_codes}")
                nodes_formatted = ",".join([f"{{{c.strip()}}}" for c in country_codes.split(',')])
                controller.set_conf('ExitNodes', nodes_formatted)
                controller.set_conf('StrictNodes', '1')
            else:
                print(f"   [TOR CONFIG] Reset géographique (Monde entier)")
                controller.reset_conf('ExitNodes')
                controller.reset_conf('StrictNodes')

            # Force le changement de circuit pour appliquer
            controller.signal(Signal.NEWNYM)
            print("   [TOR] Circuit renouvelé. Stabilisation (10s)...")
            time.sleep(10)
            
    except Exception as e:
        print(f"   [TOR ERROR] Erreur config Tor : {e}")
        # On ne bloque pas le script, on essaie quand même

def renew_tor_ip():
    """Force simplement une nouvelle IP (sans changer la config pays)."""
    print("   [TOR] --- Rotation d'IP ---")
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        time.sleep(10)
    except Exception as e:
        print(f"   [TOR ERROR] : {e}")

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

    with open(current_log_file, "a") as log: log.write(f"{vid_id}\n")
    return True

def run():
    try:
        print("--- Démarrage du script (Tor Intégral) ---")
        global_proxy_url = os.environ.get('YOUTUBE_PROXY') # Doit être socks5://127.0.0.1:9050
        
        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, 'r') as f: raw_config = json.load(f)
        
        try:
            g = Github(os.environ['GITHUB_TOKEN'])
            repo = g.get_repo(REPO_NAME)
            release = get_or_create_release(repo)
        except Exception as e: print(f"Erreur GitHub: {e}"); return

        # Options de base : TOR PAR DÉFAUT POUR TOUT LE MONDE
        # Cela évite le blocage "Datacenter IP" de GitHub Actions
        base_opts = {
            'quiet': False, 
            'no_warnings': True, 
            'socket_timeout': 30,
            'nocheckcertificate': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
            'proxy': global_proxy_url 
        }

        grouped_feeds = {}
        for item in raw_config:
            fname = item.get('filename')
            if fname not in grouped_feeds: grouped_feeds[fname] = []
            grouped_feeds[fname].append(item)

        for filename, sources in grouped_feeds.items():
            print(f"\n=== FLUX : {filename} ===")
            main_config = sources[0]
            current_log_file = f"log_{filename}.txt"
            
            downloaded_ids = []
            if os.path.exists(current_log_file):
                with open(current_log_file, "r") as f: downloaded_ids = f.read().splitlines()

            # --- GESTION GÉO TOR ---
            # On vérifie si on a besoin d'un pays spécifique
            tor_exit_nodes = main_config.get('tor_exit_nodes') # Ex: "BE,FR"
            
            if tor_exit_nodes:
                configure_tor_nodes(tor_exit_nodes)
            else:
                # Si pas de pays spécifique, on reset (Monde entier)
                # Important pour ne pas rester bloqué en Belgique si le flux précédent l'était
                configure_tor_nodes(None)

            print(f"   [DEBUG] IP utilisée : {get_tor_info()}")

            # 1. SCAN
            missing_entries = []
            auto_title = None
            
            scan_opts = base_opts.copy()
            scan_opts['extract_flat'] = True
            scan_opts['ignoreerrors'] = True
            
            with yt_dlp.YoutubeDL(scan_opts) as ydl_scan:
                for source in sources:
                    try:
                        info = ydl_scan.extract_info(source['url'], download=False)
                        if info and 'entries' in info:
                            for entry in info['entries']:
                                if entry and entry.get('id') and entry['id'] not in downloaded_ids:
                                    missing_entries.append(entry)
                    except Exception as e: print(f"Scan Warning: {e}")

            print(f"Vidéos manquantes : {len(missing_entries)}")
            batch_to_process = missing_entries[:SEARCH_WINDOW_SIZE]

            # 2. RSS (Code standard inchangé)
            fg = FeedGenerator(); fg.load_extension('podcast')
            if os.path.exists(filename): 
                try: fg.parse_file(filename)
                except: pass
            
            final_title = main_config.get('podcast_name') or f'Podcast {filename}'
            fg.title(final_title)
            fg.description(main_config.get('podcast_description') or 'Generated Feed')
            fg.link(href=f'https://github.com/{REPO_NAME}', rel='alternate')
            if main_config.get('cover_image'): 
                fg.podcast.itunes_image(main_config['cover_image'])
                fg.image(url=main_config['cover_image'], title=final_title, link=f'https://github.com/{REPO_NAME}')

            # 3. DOWNLOAD
            dl_opts = base_opts.copy()
            dl_opts.update({
                'format': 'bestaudio/best', 
                'outtmpl': '%(id)s.%(ext)s', 
                'writethumbnail': True,
                'ignoreerrors': False,
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
                max_attempts = 3
                
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
                        print(f"   [ERREUR] {str(e)[:100]}...")
                        cleanup_files(vid_id)
                        
                        # LOGIQUE D'ERREUR
                        if "private video" in err_str or "removed" in err_str:
                            print("      -> Vidéo Inaccessible. Skip.")
                            break
                        
                        elif "country" in err_str:
                            print("      -> Géo-blocage. Tentative nouvelle IP...")
                            renew_tor_ip()
                            attempts += 1

                        elif "403" in err_str or "bot" in err_str or "sign in" in err_str:
                            print("      -> Détection Bot. Rotation IP...")
                            renew_tor_ip()
                            attempts += 1
                        
                        else:
                            time.sleep(5)
                            attempts += 1

            fg.rss_file(filename)
            print(f"XML {filename} mis à jour.")

    finally:
        if os.path.exists(COOKIE_FILE): os.remove(COOKIE_FILE)

if __name__ == "__main__":
    run()
