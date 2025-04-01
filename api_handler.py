# comfyui_folder_server/api_handler.py

import asyncio
import json
import logging
import os
import shutil
import time # Added for notifications
from pathlib import Path

import aiofiles
from aiohttp import web

from .file_utils import (
    validate_and_resolve_path,
    get_relative_path,
    list_directory_contents, # Updated function
    get_base_path,
    get_file_metadata, # Updated function
)
from .ws_handler import ws_handler_instance # To notify after changes

logger = logging.getLogger(__name__)

# --- Helper for JSON Responses --- (remain the same)
def json_response(data: dict | list, status: int = 200):
    return web.json_response(data, status=status)

def json_error(message: str, status: int = 400, error_code: str | None = None):
    # ... (implementation unchanged) ...
    error_data = {"error": {"message": message}}
    if error_code:
        error_data["error"]["code"] = error_code
    logger.warning(f"API Error ({status}): {message}" + (f" Code: {error_code}" if error_code else ""))
    return web.json_response(error_data, status=status)

# --- API Handlers ---

async def handle_list_folder(request: web.Request):
    """Handler for GET /folder_server/list/{folder_type}"""
    folder_type = request.match_info.get('folder_type')
    if folder_type not in ['input', 'output']:
        return json_error("Invalid folder_type. Must be 'input' or 'output'.", 400)

    # Extract query parameters
    query = request.query
    subfolder = query.get('subfolder', None)
    # Get refresh flag for cache control
    refresh = query.get('refresh', 'false').lower() == 'true'

    try:
        page = int(query.get('page', '1'))
        limit = int(query.get('limit', '100'))
        if limit < 0: limit = 0
    except ValueError:
        return json_error("Invalid 'page' or 'limit' parameter. Must be integers.", 400)

    # Boolean flags
    dirs_only = query.get('dirs_only', 'false').lower() == 'true'
    files_only = query.get('files_only', 'false').lower() == 'true'

    # List filters
    extensions_str = query.get('extensions')
    extensions = [ext.strip().lower() for ext in extensions_str.split(',')] if extensions_str else None # Lowercase extensions here
    exclude_extensions_str = query.get('exclude_extensions')
    exclude_extensions = [ext.strip().lower() for ext in exclude_extensions_str.split(',')] if exclude_extensions_str else None # Lowercase

    # Text filter
    filename_filter = query.get('filter', None)

    # Sorting
    sort_by = query.get('sort_by', 'name')
    # Expanded valid sort fields
    valid_sort_fields = ["name", "size", "modified", "created", "width", "height", "duration", "depth", "vertices", "faces"]
    if sort_by not in valid_sort_fields:
        logger.warning(f"Invalid sort_by field '{sort_by}', defaulting to 'name'.")
        sort_by = "name"
    sort_order = query.get('sort_order', 'asc')
    if sort_order not in ['asc', 'desc']:
        sort_order = 'asc'

    try:
        # logger.info(f"Listing folder: {folder_type}/{subfolder or ''} [refresh={refresh}]") # Log refresh status

        # Core listing logic delegated to file_utils, passing refresh flag
        items, pagination, sort, filters = await list_directory_contents(
            folder_type=folder_type,
            subfolder=subfolder,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
            refresh=refresh, # Pass refresh flag
            dirs_only=dirs_only,
            files_only=files_only,
            extensions=extensions,
            exclude_extensions=exclude_extensions,
            filename_filter=filename_filter,
        )

        response_data = {
            "files": items,
            "pagination": pagination,
            "sort": sort,
            "filters_applied": filters,
        }
        return json_response(response_data)

    except FileNotFoundError as e:
        return json_error(f"Directory not found: {e}", 404, "DIRECTORY_NOT_FOUND")
    except ValueError as e: # For path validation errors
        return json_error(str(e), 400, "INVALID_PATH")
    except Exception as e:
        logger.error(f"Error listing folder {folder_type}/{subfolder}: {e}", exc_info=True)
        return json_error("An internal server error occurred while listing the folder.", 500, "LISTING_ERROR")


# --- handle_get_file, handle_upload_file, handle_delete_file ---
# --- handle_create_dir, handle_move_rename, handle_copy ---
# These handlers remain largely the same as the previous modular version.
# They use validate_and_resolve_path and the basic file operations.
# Crucially, the file watcher (started separately) and its cache invalidation
# handle the side effects needed after these operations complete successfully.
# We will ensure notifications are still sent.

async def handle_get_file(request: web.Request):
    # ... (implementation unchanged from previous modular version) ...
    folder_type = request.match_info.get('folder_type')
    file_path_rel = request.match_info.get('file_path')
    if folder_type not in ['input', 'output']: return json_error("Invalid folder_type.", 400)
    if not file_path_rel: return json_error("File path is required.", 400)
    try:
        full_path = validate_and_resolve_path(folder_type, file_path_rel)
        if not await asyncio.to_thread(full_path.exists): return json_error("File not found.", 404, "FILE_NOT_FOUND")
        if await asyncio.to_thread(full_path.is_dir): return json_error("Path points to a directory.", 400, "PATH_IS_DIRECTORY")
        logger.info(f"Serving file: {full_path}")
        return web.FileResponse(full_path)
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied.", 403, "PERMISSION_DENIED")
    except Exception as e:
        logger.error(f"Error serving file {folder_type}/{file_path_rel}: {e}", exc_info=True)
        return json_error("Internal server error.", 500, "SERVER_ERROR")

async def handle_upload_file(request: web.Request):
    # Keep the robust async upload from the modular version
    folder_type = request.match_info.get('folder_type')
    if folder_type not in ['input', 'output']: return json_error("Invalid folder_type.", 400)
    reader = None
    try: reader = await request.multipart()
    except Exception as e: return json_error("Invalid multipart request.", 400)
    subfolder = None
    file_field = None
    filename = None
    destination_path = None
    temp_upload_path = None
    async for field in reader:
        if field.name == 'file':
            if not field.filename: continue
            file_field = field
            filename = os.path.basename(field.filename)
            if not filename: return json_error("Invalid filename.", 400)
        elif field.name == 'subfolder':
            subfolder = await field.text()
            subfolder = subfolder.strip() if subfolder else None
    if not file_field or not filename: return json_error("Missing 'file' field.", 400)
    try:
        target_dir_path = validate_and_resolve_path(folder_type, subfolder)
        if not await asyncio.to_thread(target_dir_path.exists):
             await asyncio.to_thread(os.makedirs, target_dir_path, exist_ok=True)
        elif not await asyncio.to_thread(target_dir_path.is_dir):
             return json_error("Target subfolder path is not a directory.", 400)
        destination_path = target_dir_path / filename
        temp_upload_path = destination_path.with_suffix(destination_path.suffix + ".uploading")
        logger.info(f"Uploading '{filename}' to {folder_type}/{subfolder or ''} via temp {temp_upload_path.name}")
        async with aiofiles.open(temp_upload_path, 'wb') as f:
            while True:
                chunk = await file_field.read_chunk(8192) # Read in chunks
                if not chunk: break
                await f.write(chunk)
        await asyncio.to_thread(os.rename, temp_upload_path, destination_path)
        logger.info(f"Upload complete: {destination_path}")
        metadata = await get_file_metadata(destination_path, folder_type)
        # Watcher should trigger notification automatically. Manual broadcast here is redundant.
        # Consider if watcher might miss rapid create+metadata fetch? Unlikely with debounce.
        return json_response({"message": "File uploaded successfully", "file": metadata}, status=201)
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied during upload.", 403, "PERMISSION_DENIED")
    except Exception as e:
        logger.error(f"Error uploading {filename}: {e}", exc_info=True)
        if temp_upload_path and await asyncio.to_thread(os.path.exists, temp_upload_path):
             try: await asyncio.to_thread(os.remove, temp_upload_path)
             except Exception as ce: logger.error(f"Error cleaning up temp file {temp_upload_path}: {ce}")
        return json_error("Internal server error during upload.", 500, "UPLOAD_ERROR")

async def handle_delete_file(request: web.Request):
    # ... (implementation unchanged from previous modular version, relies on watcher for notification) ...
    folder_type = request.match_info.get('folder_type')
    file_path_rel = request.match_info.get('file_path')
    if folder_type not in ['input', 'output']: return json_error("Invalid folder_type.", 400)
    if not file_path_rel: return json_error("File path is required.", 400)
    try:
        full_path = validate_and_resolve_path(folder_type, file_path_rel)
        if not await asyncio.to_thread(full_path.exists): return json_error("File or directory not found.", 404, "NOT_FOUND")
        loop = asyncio.get_running_loop()
        is_directory = await loop.run_in_executor(None, full_path.is_dir)
        logger.info(f"Attempting to delete {'directory' if is_directory else 'file'}: {full_path}")
        if is_directory: await loop.run_in_executor(None, shutil.rmtree, full_path)
        else: await loop.run_in_executor(None, os.remove, full_path)
        logger.info(f"Successfully deleted: {full_path}")
        # Watcher handles notification
        return json_response({"message": f"{'Directory' if is_directory else 'File'} deleted successfully."}, status=200)
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied during deletion.", 403, "PERMISSION_DENIED")
    except OSError as e: return json_error(f"Could not delete: {e.strerror}", 500, "DELETE_OS_ERROR")
    except Exception as e:
        logger.error(f"Error deleting {folder_type}/{file_path_rel}: {e}", exc_info=True)
        return json_error("Internal server error during deletion.", 500, "DELETE_ERROR")

async def handle_create_dir(request: web.Request):
    # ... (implementation unchanged from previous modular version, relies on watcher for notification) ...
    folder_type = request.match_info.get('folder_type')
    if folder_type not in ['input', 'output']: return json_error("Invalid folder_type.", 400)
    try: data = await request.json(); dir_path_rel = data.get('path');
    except json.JSONDecodeError: return json_error("Invalid JSON body.", 400)
    if not dir_path_rel: return json_error("Missing 'path' in JSON body.", 400)
    try:
        full_path = validate_and_resolve_path(folder_type, dir_path_rel)
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, full_path.exists):
             if await loop.run_in_executor(None, full_path.is_dir):
                  metadata = await get_file_metadata(full_path, folder_type) # Get existing metadata
                  return json_response({"message": "Directory already exists.", "directory": metadata}, status=200) # OK, already exists
             else: return json_error("Path exists but is not a directory.", 409, "CONFLICT_PATH_EXISTS")
        logger.info(f"Creating directory: {full_path}")
        await loop.run_in_executor(None, os.makedirs, full_path, exist_ok=True)
        logger.info(f"Successfully created directory: {full_path}")
        metadata = await get_file_metadata(full_path, folder_type) # Get metadata after creation
        # Watcher handles notification
        return json_response({"message": "Directory created successfully.", "directory": metadata}, status=201)
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied creating directory.", 403, "PERMISSION_DENIED")
    except Exception as e:
        logger.error(f"Error creating directory {folder_type}/{dir_path_rel}: {e}", exc_info=True)
        return json_error("Internal server error creating directory.", 500, "CREATE_DIR_ERROR")

# Helper unchanged
def _parse_move_copy_path(path_str: str) -> tuple[str, str]:
    parts = path_str.split('/', 1)
    if len(parts) == 2 and parts[0] in ['input', 'output']:
        return parts[0], parts[1]
    raise ValueError(f"Invalid path format: '{path_str}'. Must be 'input/...' or 'output/...'.")


async def handle_move_rename(request: web.Request):
    # ... (implementation unchanged from previous modular version, relies on watcher for notification) ...
    # Note: Overwrite limitation still exists.
    try: data = await request.json(); source_str = data.get('source'); destination_str = data.get('destination')
    except json.JSONDecodeError: return json_error("Invalid JSON body.", 400)
    if not source_str or not destination_str: return json_error("Missing 'source' or 'destination'.", 400)
    try:
        src_folder_type, src_rel_path = _parse_move_copy_path(source_str)
        dest_folder_type, dest_rel_path = _parse_move_copy_path(destination_str)
        src_full_path = validate_and_resolve_path(src_folder_type, src_rel_path)
        dest_full_path = validate_and_resolve_path(dest_folder_type, dest_rel_path)
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, src_full_path.exists): return json_error("Source path does not exist.", 404, "SOURCE_NOT_FOUND")
        if await loop.run_in_executor(None, dest_full_path.exists):
             # Basic check - prevent overwrite unless exact same resolved path (case change)
             if src_full_path.resolve() != dest_full_path.resolve():
                  return json_error("Destination path already exists.", 409, "DESTINATION_EXISTS")
        dest_parent_dir = dest_full_path.parent
        if not await loop.run_in_executor(None, dest_parent_dir.is_dir):
             await loop.run_in_executor(None, os.makedirs, dest_parent_dir, exist_ok=True)
        logger.info(f"Moving: {src_full_path} -> {dest_full_path}")
        await loop.run_in_executor(None, shutil.move, str(src_full_path), str(dest_full_path))
        logger.info("Move/Rename successful.")
        metadata = await get_file_metadata(dest_full_path, dest_folder_type)
        # Watcher handles notification
        return json_response({"message": "Move/Rename successful.", "destination": metadata})
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied during move.", 403, "PERMISSION_DENIED")
    except Exception as e:
        logger.error(f"Error moving {source_str} to {destination_str}: {e}", exc_info=True)
        return json_error("Internal server error during move.", 500, "MOVE_ERROR")

async def handle_copy(request: web.Request):
    # ... (implementation unchanged from previous modular version, relies on watcher for notification) ...
    # Note: Overwrite limitation still exists.
    try: data = await request.json(); source_str = data.get('source'); destination_str = data.get('destination')
    except json.JSONDecodeError: return json_error("Invalid JSON body.", 400)
    if not source_str or not destination_str: return json_error("Missing 'source' or 'destination'.", 400)
    try:
        src_folder_type, src_rel_path = _parse_move_copy_path(source_str)
        dest_folder_type, dest_rel_path = _parse_move_copy_path(destination_str)
        src_full_path = validate_and_resolve_path(src_folder_type, src_rel_path)
        dest_full_path = validate_and_resolve_path(dest_folder_type, dest_rel_path)
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, src_full_path.exists): return json_error("Source path does not exist.", 404, "SOURCE_NOT_FOUND")
        if await loop.run_in_executor(None, dest_full_path.exists): return json_error("Destination path already exists.", 409, "DESTINATION_EXISTS")
        dest_parent_dir = dest_full_path.parent
        if not await loop.run_in_executor(None, dest_parent_dir.is_dir):
             await loop.run_in_executor(None, os.makedirs, dest_parent_dir, exist_ok=True)
        is_directory = await loop.run_in_executor(None, src_full_path.is_dir)
        logger.info(f"Copying {'directory' if is_directory else 'file'}: {src_full_path} -> {dest_full_path}")
        if is_directory: await loop.run_in_executor(None, shutil.copytree, str(src_full_path), str(dest_full_path), dirs_exist_ok=False)
        else: await loop.run_in_executor(None, shutil.copy2, str(src_full_path), str(dest_full_path))
        logger.info("Copy successful.")
        metadata = await get_file_metadata(dest_full_path, dest_folder_type)
        # Watcher handles notification
        return json_response({"message": "Copy successful.", "destination": metadata})
    except ValueError as e: return json_error(str(e), 400, "INVALID_PATH")
    except PermissionError: return json_error("Permission denied during copy.", 403, "PERMISSION_DENIED")
    except FileExistsError: return json_error("Destination path already exists.", 409, "DESTINATION_EXISTS")
    except Exception as e:
        logger.error(f"Error copying {source_str} to {destination_str}: {e}", exc_info=True)
        return json_error("Internal server error during copy.", 500, "COPY_ERROR")
        