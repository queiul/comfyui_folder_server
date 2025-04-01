# comfyui_folder_server/file_utils.py

import os
import time
import mimetypes
import logging
import asyncio
import threading
from pathlib import Path
from stat import S_ISDIR

import folder_paths # ComfyUI specific

# Imports for new metadata extraction
from PIL import Image
try:
    from moviepy.video.io.VideoFileClip import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False
    print("[Folder Server] Warning: moviepy not found. Video metadata extraction disabled.")
try:
    import trimesh
    TRIMESH_AVAILABLE = True
except ImportError:
    TRIMESH_AVAILABLE = False
    print("[Folder Server] Warning: trimesh not found. 3D model metadata extraction disabled.")


logger = logging.getLogger(__name__)

# --- Caching Globals ---
file_listing_cache = {}
cache_lock = threading.Lock()
CACHE_TIMEOUT = 15  # Increased timeout slightly
MAX_CACHE_ENTRIES = 200 # Increased max entries
CACHE_CLEANUP_INTERVAL = 300  # 5 minutes in seconds
_cache_cleanup_thread = None
_stop_cache_cleanup = threading.Event()

# --- Cache Management Functions ---

def _invalidate_cache(folder_path_str: str):
    """Invalidate the cache for a specific folder path string."""
    with cache_lock:
        if folder_path_str in file_listing_cache:
            try:
                del file_listing_cache[folder_path_str]
                # logger.debug(f"Cache invalidated for: {folder_path_str}")
            except KeyError:
                 pass # Already removed by another thread

def cleanup_cache_task():
    """Periodically clean up the file listing cache."""
    global file_listing_cache
    logger.info("Starting cache cleanup task...")
    while not _stop_cache_cleanup.is_set():
        _stop_cache_cleanup.wait(CACHE_CLEANUP_INTERVAL)
        if _stop_cache_cleanup.is_set():
            break

        try:
            with cache_lock:
                start_count = len(file_listing_cache)
                current_time = time.time()
                # Remove expired entries
                expired_keys = [
                    key for key, entry in file_listing_cache.items()
                    if current_time - entry["timestamp"] > CACHE_TIMEOUT
                ]
                for key in expired_keys:
                     try:
                        del file_listing_cache[key]
                     except KeyError:
                        pass # Already gone

                removed_expired = len(expired_keys)

                # If still too many entries, remove the oldest ones
                removed_lru = 0
                if len(file_listing_cache) > MAX_CACHE_ENTRIES:
                    sorted_entries = sorted(
                        file_listing_cache.items(),
                        key=lambda x: x[1]["timestamp"]
                    )
                    entries_to_remove = len(file_listing_cache) - MAX_CACHE_ENTRIES
                    for i in range(entries_to_remove):
                        key = sorted_entries[i][0]
                        try:
                             del file_listing_cache[key]
                             removed_lru += 1
                        except KeyError:
                             pass # Already gone

                if removed_expired > 0 or removed_lru > 0:
                    logger.info(f"Cache cleanup: Start={start_count}, Removed Expired={removed_expired}, Removed LRU={removed_lru}, End={len(file_listing_cache)}")

        except Exception as e:
            logger.error(f"Error in cache cleanup task: {e}", exc_info=True)

    logger.info("Cache cleanup task stopped.")


def start_cache_cleanup():
    """Starts the cache cleanup thread."""
    global _cache_cleanup_thread
    if _cache_cleanup_thread is None or not _cache_cleanup_thread.is_alive():
        _stop_cache_cleanup.clear()
        _cache_cleanup_thread = threading.Thread(target=cleanup_cache_task, daemon=True, name="FolderServerCacheCleanup")
        _cache_cleanup_thread.start()
        logger.info("Folder Server cache cleanup thread started.")

def stop_cache_cleanup():
    """Stops the cache cleanup thread."""
    global _cache_cleanup_thread
    logger.info("Stopping cache cleanup thread...")
    _stop_cache_cleanup.set()
    if _cache_cleanup_thread and _cache_cleanup_thread.is_alive():
         _cache_cleanup_thread.join(timeout=5) # Wait briefly
    _cache_cleanup_thread = None
    logger.info("Cache cleanup thread signaled to stop.")


async def get_cached_directory_listing(folder_path: Path, force_refresh: bool = False) -> list[os.DirEntry]:
    """
    Get cached directory listing or scan the directory if needed. Runs scan in executor.
    Returns a list of os.DirEntry objects.
    """
    global file_listing_cache
    cache_key = str(folder_path.resolve()) # Use resolved absolute path as key
    loop = asyncio.get_running_loop()

    if not force_refresh:
        with cache_lock:
            cache_entry = file_listing_cache.get(cache_key)
            current_time = time.time()
            # Check if we have a valid cache entry
            if cache_entry and (current_time - cache_entry["timestamp"] < CACHE_TIMEOUT):
                # logger.debug(f"Cache hit for: {cache_key}")
                return cache_entry["entries"]
            # logger.debug(f"Cache miss or expired for: {cache_key}")

    # Scan the directory (blocking call) and update cache
    try:
        # Run os.scandir within an executor thread
        entries = await loop.run_in_executor(None, list, os.scandir(folder_path))
        with cache_lock:
            file_listing_cache[cache_key] = {
                "entries": entries,
                "timestamp": time.time()
            }
        # logger.debug(f"Cache updated for: {cache_key} with {len(entries)} entries")
        return entries
    except FileNotFoundError:
         _invalidate_cache(cache_key) # Ensure invalid path isn't cached
         raise # Re-raise the original error
    except Exception as e:
        logger.error(f"Error scanning directory {folder_path}: {e}")
        _invalidate_cache(cache_key) # Invalidate on error
        raise # Re-raise


# --- Path Validation & Resolution --- (Same as before)
def get_base_path(folder_type: str) -> Path:
    # ... (implementation unchanged) ...
    if folder_type == "input":
        return Path(folder_paths.get_input_directory()).resolve()
    elif folder_type == "output":
        return Path(folder_paths.get_output_directory()).resolve()
    else:
        raise ValueError(f"Invalid folder_type: {folder_type}")

def validate_and_resolve_path(folder_type: str, subpath: str | None) -> Path:
    # ... (implementation unchanged) ...
    base_path = get_base_path(folder_type)
    if not subpath:
        return base_path
    try:
        # Use resolve(strict=False) initially for paths that might not exist yet (like for creation)
        # Then check existence later if needed by the operation.
        full_path = (base_path / subpath).resolve(strict=False)
    except Exception as e: # Catch potential errors during resolution if path is malformed
         logger.warning(f"Path resolution failed for subpath '{subpath}': {e}")
         raise ValueError(f"Invalid subpath: {subpath}")

    # Re-check containment after resolving potential '..'
    # Check against the resolved base path
    resolved_base = base_path.resolve(strict=True) # Base must exist
    if resolved_base not in full_path.parents and full_path != resolved_base:
        logger.warning(f"Path traversal attempt detected: {folder_type}/{subpath} -> {full_path}")
        raise ValueError("Path traversal attempt detected.")

    return full_path

def get_relative_path(folder_type: str, full_path: Path) -> str:
    # ... (implementation unchanged) ...
    base_path = get_base_path(folder_type)
    try:
        relative = full_path.relative_to(base_path)
        return str(relative).replace("\\", "/")
    except ValueError:
        logger.error(f"Could not get relative path for {full_path} against base {base_path}")
        raise ValueError(f"Path {full_path} is not relative to {base_path}")

# --- File Metadata Extraction (Updated) ---

# Helper functions for metadata (run blocking parts in executor)
def _get_image_dimensions_sync(file_path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(file_path) as img:
            return img.width, img.height
    except Exception as e:
        # logger.debug(f"Could not get image dimensions for {file_path}: {e}")
        return None, None

def _get_video_metadata_sync(file_path: Path) -> dict | None:
    if not MOVIEPY_AVAILABLE:
        return None
    try:
        # moviepy can be slow to load the clip
        clip = VideoFileClip(str(file_path)) # moviepy might prefer string paths
        metadata = {
            "width": int(clip.size[0]),
            "height": int(clip.size[1]),
            "duration": float(clip.duration)
        }
        clip.close() # Release file handle
        return metadata
    except Exception as e:
        # logger.debug(f"Could not get video metadata for {file_path}: {e}")
        return None

def _get_3d_model_metadata_sync(file_path: Path) -> dict | None:
    if not TRIMESH_AVAILABLE:
        return None
    try:
        # trimesh loading can be slow and memory intensive
        mesh = trimesh.load(str(file_path), force='mesh') # Force loading as mesh

        metadata = {}
        # Get bounding box dimensions
        try:
            # Use bounds directly for simplicity, extent might be more accurate but varies
            bounds = mesh.bounds
            dimensions = bounds[1] - bounds[0]  # max - min
            metadata["width"] = float(dimensions[0]) # X
            metadata["height"] = float(dimensions[1]) # Y
            metadata["depth"] = float(dimensions[2]) # Z
        except Exception as be:
             logger.debug(f"Could not calculate bounds for {file_path}: {be}")

        # Add vertex/face count
        try:
            metadata["vertices"] = len(mesh.vertices)
            metadata["faces"] = len(mesh.faces)
        except Exception as vfe:
             logger.debug(f"Could not get vertex/face count for {file_path}: {vfe}")

        return metadata if metadata else None # Return None if no info could be extracted
    except ValueError as ve: # Trimesh often raises ValueError for unsupported formats
        logger.debug(f"Trimesh failed to load {file_path} (likely unsupported format): {ve}")
        return None
    except Exception as e:
        # logger.debug(f"Could not get 3D model metadata for {file_path}: {e}")
        return None


async def get_file_metadata(file_path: Path, folder_type: str) -> dict | None:
    """
    Gathers metadata for a file entry. Runs potentially slow I/O in executor.
    """
    try:
        loop = asyncio.get_running_loop()
        # Run os.stat in executor as it's blocking I/O
        stat_result = await loop.run_in_executor(None, os.stat, file_path)
        is_dir = S_ISDIR(stat_result.st_mode)

        # Use resolved path for relative path calculation if possible
        resolved_path = file_path.resolve()
        relative_p = get_relative_path(folder_type, resolved_path)

        data = {
            "name": file_path.name,
            "path": f"{folder_type}/{relative_p}",
            "is_dir": is_dir,
            "size": stat_result.st_size,
            "modified": stat_result.st_mtime,
            "created": stat_result.st_ctime,
        }

        if not is_dir:
            ext = file_path.suffix.lower()
            metadata = None
            if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                dims = await loop.run_in_executor(None, _get_image_dimensions_sync, file_path)
                if dims and dims[0] is not None:
                    metadata = {"width": dims[0], "height": dims[1]}
            elif MOVIEPY_AVAILABLE and ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv']:
                 metadata = await loop.run_in_executor(None, _get_video_metadata_sync, file_path)
            elif TRIMESH_AVAILABLE and ext in ['.obj', '.stl', '.ply', '.glb', '.gltf', '.fbx', '.dae', '.3ds']:
                 metadata = await loop.run_in_executor(None, _get_3d_model_metadata_sync, file_path)

            if metadata:
                data.update(metadata)

        return data
    except FileNotFoundError:
        logger.warning(f"File not found during metadata fetch: {file_path}")
        return None
    except Exception as e:
        logger.error(f"Error getting metadata for {file_path}: {e}", exc_info=True)
        return None


# --- List Directory Logic (Updated) ---

async def list_directory_contents(
    folder_type: str,
    subfolder: str | None = None,
    sort_by: str = "name",
    sort_order: str = "asc",
    page: int = 1,
    limit: int = 100,
    refresh: bool = False, # Added refresh parameter
    dirs_only: bool = False,
    files_only: bool = False,
    extensions: list[str] | None = None,
    exclude_extensions: list[str] | None = None,
    filename_filter: str | None = None,
) -> tuple[list[dict], dict, dict, dict]:
    """
    Lists directory contents with filtering, sorting, and pagination. Uses cache.
    Returns: (items_on_page, pagination_info, sort_info, filters_applied)
    """
    try:
        # Validate target path, ensuring it exists and is a directory for listing
        target_path = validate_and_resolve_path(folder_type, subfolder)
        if not await asyncio.to_thread(target_path.is_dir):
            # Use asyncio.to_thread for is_dir check as it involves stat
            raise FileNotFoundError(f"Directory not found or path is not a directory: {target_path}")
    except (ValueError, FileNotFoundError) as e:
        logger.error(f"Error resolving path for listing {folder_type}/{subfolder}: {e}")
        raise # Re-raise to be caught by the API handler

    # Get directory entries (potentially from cache)
    try:
        dir_entries = await get_cached_directory_listing(target_path, force_refresh=refresh)
    except FileNotFoundError as e: # Should be caught above, but double-check
        logger.error(f"Directory not found during listing: {target_path}")
        raise e
    except Exception as e:
        logger.error(f"Error listing directory {target_path}: {e}", exc_info=True)
        raise # Re-raise

    # --- Filtering (Applied before getting full metadata for efficiency) ---
    filtered_paths = []
    allowed_extensions = {f".{ext.lower().strip()}" for ext in extensions} if extensions else None
    excluded_extensions = {f".{ext.lower().strip()}" for ext in exclude_extensions} if exclude_extensions else None
    filter_lower = filename_filter.lower() if filename_filter else None

    for entry in dir_entries:
        is_entry_dir = entry.is_dir() # Check type once

        # Filter by type (dirs/files only)
        if dirs_only and not is_entry_dir:
            continue
        if files_only and is_entry_dir:
            continue

        # Filter by filename text (case-insensitive)
        if filter_lower and filter_lower not in entry.name.lower():
            continue

        # Filter by extension (only for files)
        if not is_entry_dir:
            ext_lower = Path(entry.name).suffix.lower()
            if allowed_extensions and ext_lower not in allowed_extensions:
                continue
            if excluded_extensions and ext_lower in excluded_extensions:
                continue

        filtered_paths.append(Path(entry.path)) # Store Path objects for metadata call


    # --- Get Metadata for filtered items ---
    tasks = [get_file_metadata(path, folder_type) for path in filtered_paths]
    all_items_metadata = await asyncio.gather(*tasks)
    # Filter out None results from potential errors during metadata fetch
    valid_items = [item for item in all_items_metadata if item is not None]


    # --- Sorting (on full metadata) ---
    reverse = sort_order.lower() == "desc"
    # Update valid sort fields to include new metadata keys potentially
    valid_sort_fields = ["name", "size", "modified", "created", "width", "height", "duration", "depth", "vertices", "faces"]

    def sort_key(item):
        # Use requested sort_by if valid and present, otherwise default to name
        sort_field = sort_by if sort_by in valid_sort_fields and sort_by in item else "name"
        key_value = item.get(sort_field)

        # Handle None values appropriately for sorting
        if key_value is None:
            # Sort None as smallest for numbers/dates, empty string for text
            if sort_field in ["size", "modified", "created", "width", "height", "duration", "depth", "vertices", "faces"]:
                return -float('inf') if reverse else float('inf') # Treat None as smallest/largest depending on order
            else: # name or other text
                return ""

        # Case-insensitive sort for name
        if sort_field == "name":
             return str(key_value).lower()

        return key_value

    try:
        valid_items.sort(key=sort_key, reverse=reverse)
    except TypeError as e:
        logger.warning(f"Sorting failed (likely mixed types for key '{sort_by}'): {e}. Falling back to name sort.")
        sort_by = "name" # Update effective sort field
        valid_items.sort(key=lambda item: str(item.get("name","")).lower(), reverse=reverse)

    sort_info = {"field": sort_by, "order": sort_order}
    filters_applied = {
        "dirs_only": dirs_only,
        "files_only": files_only,
        "extensions": extensions,
        "exclude_extensions": exclude_extensions,
        "filename_filter": filename_filter,
    }

    # --- Pagination ---
    total_items = len(valid_items)
    limit = max(0, limit) # Ensure limit is not negative
    total_pages = (total_items + limit - 1) // limit if limit > 0 else 1
    page = max(1, min(page, total_pages)) if total_pages > 0 else 1 # Clamp page number
    start_index = (page - 1) * limit if limit > 0 else 0
    end_index = start_index + limit if limit > 0 else total_items

    items_on_page = valid_items[start_index:end_index]

    pagination_info = {
        "total": total_items,
        "page": page,
        "limit": limit if limit > 0 else total_items,
        "total_pages": total_pages,
    }

    return items_on_page, pagination_info, sort_info, filters_applied
    