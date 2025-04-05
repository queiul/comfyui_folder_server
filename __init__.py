# comfyui_folder_server/__init__.py

import subprocess
import sys
import importlib.util
import os
import asyncio
import logging
import atexit
import threading # Import threading for the init lock

# --- Dependency Checks ---
def install_package(package_name, version_spec=""):
    """Installs a package using pip."""
    print(f"[Folder Server] Installing {package_name}...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", f"{package_name}{version_spec}"])
        print(f"[Folder Server] Successfully installed {package_name}.")
    except subprocess.CalledProcessError as e:
        print(f"[Folder Server] ERROR: Failed to install {package_name}. Please install it manually.")
        print(e)
    except Exception as e:
        print(f"[Folder Server] ERROR: An unexpected error occurred during installation of {package_name}.")
        print(e)

required_packages = {
    "watchdog": ">=3.0.0",
    "Pillow": ">=9.0.0",
    "moviepy": ">=1.0.3",
    "trimesh": ">=3.9.0", # Requires numpy, etc. implicitly
    "aiofiles": ">=23.1.0" # Recommended
}

for package, version in required_packages.items():
    if importlib.util.find_spec(package.split('[')[0]) is None: # Handle extras like 'trimesh[easy]' if needed
        install_package(package, version)
    # else: # Optional: Check version if already installed
    #    pass


# --- Module Imports (after potential installs) ---
try:
    import server # ComfyUI internal server instance
    from . import api_handler
    from . import ws_handler
    from . import file_watcher
    from . import file_utils
except ImportError as e:
     print(f"[Folder Server] Failed to import required modules after installation check: {e}")
     raise e

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Globals for Lazy Initialization ---
_folder_server_initialized = False
_init_lock = asyncio.Lock() # Use asyncio Lock for async context
_main_event_loop = None

# --- ComfyUI Registration --- (Keep as is)
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = None

# --- Initialization Function ---
async def ensure_folder_server_initialized():
    """Initialize file watcher, cache cleanup, etc., if not already done."""
    global _folder_server_initialized, _main_event_loop
    if _folder_server_initialized:
        return # Already initialized

    async with _init_lock:
        # Double-check after acquiring lock, in case another task initialized
        if _folder_server_initialized:
            return

        logger.info("Initializing Folder Server addon (Lazy)...")
        try:
            _main_event_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("Could not get running event loop during lazy initialization. Folder Server cannot start.")
            # Cannot proceed without the loop
            # Should we set initialized to True to prevent retries? Or let it retry?
            # Let's prevent retries to avoid spamming logs.
            _folder_server_initialized = True # Mark as 'attempted' even if failed
            return

        # Start cache cleanup thread (this uses threading, not asyncio directly, so safe here)
        try:
             file_utils.start_cache_cleanup()
        except Exception as e:
             logger.error(f"Failed to start cache cleanup thread: {e}", exc_info=True)

        # Start file watcher, passing the broadcast method and cache invalidator
        try:
            file_watcher.start_watching(
                _main_event_loop,
                ws_handler.ws_handler_instance.broadcast,
                file_utils._invalidate_cache
            )
            logger.info("Folder Server file watcher started.")
        except Exception as e:
            logger.error(f"Failed to start file watcher: {e}", exc_info=True)
            # Proceeding even if watcher fails, core API might still work

        _folder_server_initialized = True # Mark as successfully initialized
        logger.info("Folder Server addon initialization complete.")

# --- Cleanup Function --- (Remains largely the same)
def cleanup_folder_server():
    """Stop file watcher, cache cleanup, and other resources."""
    global _folder_server_initialized
    # Check the flag ensuring cleanup only happens if init succeeded or was attempted
    if not _folder_server_initialized: # Or maybe check if threads/observers actually exist?
        logger.debug("Folder Server cleanup skipped: Initialization never completed.")
        return

    logger.info("Shutting down Folder Server addon...")
    try:
        file_watcher.stop_watching()
    except Exception as e:
        logger.error(f"Error stopping file watcher: {e}", exc_info=True)

    try:
        file_utils.stop_cache_cleanup()
    except Exception as e:
        logger.error(f"Error stopping cache cleanup: {e}", exc_info=True)

    # Any other cleanup tasks
    _folder_server_initialized = False # Reset flag
    logger.info("Folder Server addon shutdown complete.")


# --- Register Cleanup ---
# atexit runs when the Python interpreter exits normally.
# It's suitable for stopping background threads like the watcher and cache cleanup.
atexit.register(cleanup_folder_server)


# --- Server Route & WebSocket Setup ---
# Modify handlers to call ensure_folder_server_initialized

@server.PromptServer.instance.routes.get('/folder_server/list/{folder_type}')
async def get_list_folder(request):
    await ensure_folder_server_initialized()
    if not _folder_server_initialized or not file_watcher.observer: # Check if init really worked
         # Return an error if initialization failed critically (e.g., no loop)
         # Or if watcher failed and is critical. Decide based on requirements.
         # For now, let the handler try and potentially fail if deps aren't met.
         pass
    return await api_handler.handle_list_folder(request)

@server.PromptServer.instance.routes.get('/folder_server/file/{folder_type}/{file_path:.*}')
async def get_file(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_get_file(request)

@server.PromptServer.instance.routes.post('/folder_server/upload/{folder_type}')
async def post_upload_file(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_upload_file(request)

@server.PromptServer.instance.routes.delete('/folder_server/file/{folder_type}/{file_path:.*}')
async def delete_file(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_delete_file(request)

@server.PromptServer.instance.routes.post('/folder_server/create_dir/{folder_type}')
async def post_create_dir(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_create_dir(request)

@server.PromptServer.instance.routes.post('/folder_server/move')
async def post_move_rename(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_move_rename(request)

@server.PromptServer.instance.routes.post('/folder_server/copy')
async def post_copy(request):
    await ensure_folder_server_initialized()
    return await api_handler.handle_copy(request)

# WebSocket Route
@server.PromptServer.instance.routes.get('/ws/folder_server')
async def websocket_endpoint(request):
    await ensure_folder_server_initialized()
    # Pass the connection handling to the ws_handler instance
    # ws_handler itself doesn't directly depend on the watcher/cache threads
    # but the broadcast mechanism does. ensure_initialized makes sure the loop needed for broadcast is ready.
    return await ws_handler.ws_handler_instance.handle_connection(request)

# Docs Route (Doesn't strictly need initialization, but good practice)
docs_html_path = os.path.join(os.path.dirname(__file__), "docs.html")
@server.PromptServer.instance.routes.get('/folder_server/docs')
async def get_docs(request):
    # await ensure_folder_server_initialized() # Optional for docs
    from aiohttp import web
    if os.path.exists(docs_html_path):
        return web.FileResponse(docs_html_path)
    else:
        logger.warning(f"docs.html not found at: {docs_html_path}")
        return web.Response(status=404, text="Documentation file (docs.html) not found.")

# --- REMOVE direct initialization call from here ---
# try:
#     if server.PromptServer.instance:
#          initialize_folder_server() # REMOVED
#          atexit.register(cleanup_folder_server) # Keep atexit registration
#     else:
#          logger.warning("ComfyUI PromptServer instance not found at import time. Folder Server initialization deferred.")
# except Exception as e:
#      logger.error(f"Error during Folder Server initial setup: {e}", exc_info=True)

logger.info("Folder Server API routes registered (initialization deferred).")
