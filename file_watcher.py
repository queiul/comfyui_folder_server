# comfyui_folder_server/file_watcher.py

import asyncio
import logging
import time
import os
from pathlib import Path
from typing import Callable, Coroutine, Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent, DirModifiedEvent, FileModifiedEvent

import folder_paths # ComfyUI specific
from .file_utils import get_base_path, get_relative_path, _invalidate_cache as invalidate_cache_global # Import cache invalidator

logger = logging.getLogger(__name__)

# Global observer instance
observer = None
stop_event = asyncio.Event() # Use asyncio event for graceful shutdown signal

class ChangeHandler(FileSystemEventHandler):
    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 callback: Callable[[dict], Coroutine[Any, Any, None]],
                 cache_invalidator: Callable[[str], None]):
        self.loop = loop
        self.callback = callback
        self.cache_invalidator = cache_invalidator
        # Debounce cache: Store {path: timestamp}
        self._debounce_cache = {}
        self._debounce_interval = 0.25 # Slightly increased debounce

    # _should_process, _determine_folder_type - remain the same
    def _should_process(self, event_path_str: str) -> bool:
        """Basic debounce to avoid duplicate events for single operations."""
        now = time.monotonic()
        last_event_time = self._debounce_cache.get(event_path_str)
        # Special case: allow modify events even if debounced shortly after create/move
        # This helps catch metadata updates after initial file write.
        # However, watchdog might not always fire modify reliably right after creation.
        # if isinstance(event, FileModifiedEvent) and last_event_time:
        #     pass # Check if we want to allow modify anyway?

        if last_event_time and (now - last_event_time) < self._debounce_interval:
            # logger.debug(f"Debouncing event for path: {event_path_str}")
            return False # Debounce this event
        self._debounce_cache[event_path_str] = now
        # Clean up old entries occasionally (optional)
        if len(self._debounce_cache) > 1000: # Prevent indefinite growth
             cutoff = now - self._debounce_interval * 10 # Longer cleanup interval
             self._debounce_cache = {p: t for p, t in self._debounce_cache.items() if t > cutoff}
        return True

    def _determine_folder_type(self, path: Path) -> str | None:
        """Determine if the path belongs to 'input' or 'output'."""
        input_base = get_base_path("input").resolve()
        output_base = get_base_path("output").resolve()
        resolved_path = path.resolve() # Resolve the path for reliable comparison
        try:
            if input_base in resolved_path.parents or resolved_path == input_base:
                return "input"
            if output_base in resolved_path.parents or resolved_path == output_base:
                return "output"
        except Exception as e: # Handle potential errors during resolution or comparison
            logger.warning(f"Error determining folder type for {path}: {e}")
        return None

    def _schedule_callback(self, event_data: dict):
        """Safely schedule the async callback from the watchdog thread."""
        if self.callback and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.callback(event_data), self.loop)
        elif not self.loop or not self.loop.is_running():
             logger.warning("Event loop not available or not running, cannot schedule callback.")


    def process_event(self, event: FileSystemEvent, event_type_override: str | None = None):
        """Generic event processing, cache invalidation, and dispatching."""
        if isinstance(event, DirModifiedEvent): # Often noisy, ignore
            return

        # Use src_path for delete/modify/create, dest_path for move's destination impact
        path_str = event.src_path
        dest_path_str = getattr(event, 'dest_path', None)

        # --- Debounce ---
        # Use a combined key for moves to better debounce related delete/create
        debounce_key = f"{path_str}|{dest_path_str}" if event.event_type == 'moved' else path_str
        if not self._should_process(debounce_key):
              # logger.debug(f"Debouncing event for key: {debounce_key}")
              return # Debounced

        # --- Path and Type Determination ---
        src_path = Path(path_str)
        src_folder_type = self._determine_folder_type(src_path)
        dest_path = Path(dest_path_str) if dest_path_str else None
        dest_folder_type = self._determine_folder_type(dest_path) if dest_path else None

        # Ignore events outside known folders more strictly
        if not src_folder_type and event.event_type != 'moved': # Allow moved *from* outside if dest is inside? No, stick to src base.
             # logger.debug(f"Ignoring event with source outside tracked folders: {path_str}")
             return
        if event.event_type == 'moved' and not src_folder_type and not dest_folder_type:
             # logger.debug(f"Ignoring move event where both src/dest outside tracked folders: {path_str} -> {dest_path_str}")
             return

        # --- Invalidate Cache BEFORE Broadcasting ---
        try:
            parent_dir_path = src_path.parent
            parent_dir_str = str(parent_dir_path.resolve())
            # Invalidate parent of the source path
            if parent_dir_path and self._determine_folder_type(parent_dir_path): # Only invalidate if parent is tracked
                 self.cache_invalidator(parent_dir_str)

            # For moves, also invalidate the destination parent
            if dest_path:
                dest_parent_path = dest_path.parent
                dest_parent_str = str(dest_parent_path.resolve())
                if dest_parent_path and self._determine_folder_type(dest_parent_path): # Only invalidate if tracked
                    self.cache_invalidator(dest_parent_str)

            # For file modify/delete, invalidate the file's own cache entry if one existed (though less common)
            # if event.event_type in ['modified', 'deleted'] and not event.is_directory:
            #      self.cache_invalidator(str(src_path.resolve()))

        except Exception as e:
            logger.error(f"Error invalidating cache for event {event}: {e}", exc_info=True)


        # --- Prepare and Send Notification ---
        try:
            event_name_map = {
                "created": "directory_created" if event.is_directory else "file_created",
                "deleted": "directory_deleted" if event.is_directory else "file_deleted",
                "modified": "file_modified", # Only files trigger modify usually
                "moved": "directory_moved" if event.is_directory else "file_moved",
            }
            event_action = event_type_override or event_name_map.get(event.event_type)

            if not event_action:
                return # Ignore unknown event types

            # Determine primary folder_type for the event notification
            # Usually the source, unless it's a creation event
            notify_folder_type = dest_folder_type if event_action.endswith('_created') else src_folder_type
            # If folder_type is None (e.g. deleted from outside), maybe skip notification?
            if not notify_folder_type:
                # Use destination if source was None (e.g., moved from outside to inside)
                if event.event_type == 'moved' and dest_folder_type:
                     notify_folder_type = dest_folder_type
                else:
                    # logger.debug(f"Skipping notification for event with undetermined folder type: {event_action} on {path_str}")
                    return # Skip notification if we can't determine relevant folder

            # Get relative paths safely
            try:
                 rel_src_path = f"{src_folder_type}/{get_relative_path(src_folder_type, src_path)}" if src_folder_type else src_path.name
            except ValueError: # Handle cases where path might not be truly relative after all checks
                 rel_src_path = src_path.name

            data = {
                "action": event_action,
                "folder_type": notify_folder_type, # Report based on affected folder
                "is_directory": event.is_directory,
                "path": rel_src_path, # Source path for delete/move, created path for create
                "timestamp": time.time()
            }

            if event_action.endswith("_moved") and dest_path and dest_folder_type:
                 try:
                    rel_dest_path = f"{dest_folder_type}/{get_relative_path(dest_folder_type, dest_path)}" if dest_folder_type else dest_path.name
                 except ValueError:
                    rel_dest_path = dest_path.name

                 data["destination_folder_type"] = dest_folder_type
                 data["destination_path"] = rel_dest_path
                 # Override 'path' for move event to clearly be the source path
                 data["path"] = rel_src_path
                 data["folder_type"] = src_folder_type # Explicitly set source folder for move clarity

                 # Optionally add metadata of the moved item at its NEW location
                 # metadata = await get_file_metadata(dest_path, dest_folder_type) # Careful: await in sync handler! Schedule instead.
                 # This needs rethinking - metadata fetch should happen async before broadcast

            elif event_action.endswith("_created"):
                 # Optionally add metadata of the created item
                 # metadata = await get_file_metadata(src_path, notify_folder_type) # Careful: await! Schedule instead.
                 # Rethink: Broadcast simple event first, client can request details?
                 data["path"] = rel_src_path # Path where it was created

            # Schedule the broadcast
            self._schedule_callback(data)

        except Exception as e:
            logger.error(f"Error processing watchdog event {event}: {e}", exc_info=True)


    def on_created(self, event):
        self.process_event(event)

    def on_deleted(self, event):
        self.process_event(event)

    def on_modified(self, event):
        # Avoid directory modify events
        if not event.is_directory:
             self.process_event(event)

    def on_moved(self, event):
        self.process_event(event)


def start_watching(loop: asyncio.AbstractEventLoop,
                   callback: Callable[[dict], Coroutine[Any, Any, None]],
                   cache_invalidator: Callable[[str], None]): # Added cache_invalidator
    """Starts the file system watcher in a separate thread."""
    global observer, stop_event
    if observer and observer.is_alive():
        logger.warning("Watcher observer already running.")
        return

    input_path = folder_paths.get_input_directory()
    output_path = folder_paths.get_output_directory()
    os.makedirs(input_path, exist_ok=True)
    os.makedirs(output_path, exist_ok=True)

    event_handler = ChangeHandler(loop, callback, cache_invalidator) # Pass invalidator
    observer = Observer()
    # ... (rest of observer scheduling logic remains the same) ...
    try:
        observer.schedule(event_handler, input_path, recursive=True)
        logger.info(f"Watching directory: {input_path}")
    except Exception as e:
         logger.error(f"Failed to schedule watch for input path {input_path}: {e}")

    try:
        if Path(input_path).resolve() != Path(output_path).resolve():
             observer.schedule(event_handler, output_path, recursive=True)
             logger.info(f"Watching directory: {output_path}")
        else:
            logger.info(f"Input and output paths are the same ({input_path}), watching only once.")
    except Exception as e:
         logger.error(f"Failed to schedule watch for output path {output_path}: {e}")

    if not observer.emitters:
        logger.error("No paths successfully scheduled for watching. Watchdog will not start.")
        observer = None
        return

    stop_event.clear()

    # Run observer in a separate thread managed by asyncio
    def run_observer():
        # ... (implementation unchanged) ...
        global observer, stop_event
        if not observer: return
        try:
             logger.info("Starting watchdog observer thread.")
             observer.start()
             while not stop_event.is_set() and observer.is_alive():
                  time.sleep(0.5)
        except Exception as e:
             logger.error(f"Exception in watchdog observer thread: {e}", exc_info=True)
        finally:
            if observer and observer.is_alive():
                 observer.stop()
            if observer:
                 observer.join()
            logger.info("Watchdog observer thread stopped.")
            observer = None

    loop.run_in_executor(None, run_observer)
    logger.info("Watchdog observer scheduled to run.")


def stop_watching():
    """Signals the watcher thread to stop."""
    # ... (implementation unchanged) ...
    global observer, stop_event
    if observer and observer.is_alive():
        logger.info("Signalling watchdog observer to stop...")
        stop_event.set()
    else:
        logger.info("Watchdog observer not running or already stopped.")
