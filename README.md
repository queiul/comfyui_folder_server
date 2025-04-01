# ComfyUI Folder Server

**A custom node addon for ComfyUI that provides a REST API and WebSocket
interface for managing files within the ComfyUI `input` and `output`
directories.**

This addon runs in the background alongside ComfyUI, allowing external
applications, scripts, or frontends to interact with your ComfyUI files
remotely. It does _not_ add any visual nodes to the ComfyUI graph interface
itself.

## Features

- **REST API:** Provides endpoints for common file operations:
  - List directory contents (input/output folders and subfolders) with
    pagination, sorting, and filtering (by type, extension, name).
  - Download individual files.
  - Upload files (with subfolder support).
  - Delete files and directories (recursively).
  - Create new directories.
  - Move/Rename files and directories (including between input/output).
  - Copy files and directories (including between input/output).
- **Metadata Extraction:** Automatically extracts and includes metadata in
  directory listings for:
  - Images (Width, Height via Pillow)
  - Videos (Width, Height, Duration via moviepy)
  - 3D Models (Bounding Box Dimensions, Vertices, Faces via trimesh)
- **WebSocket Notifications:** Provides a WebSocket endpoint for real-time
  notifications of file system changes (create, delete, modify, move) within the
  monitored input/output folders.
- **Caching:** Implements a short-lived server-side cache for directory listings
  to improve performance for frequent requests.
- **Dependency Handling:** Attempts to automatically install required Python
  packages (`watchdog`, `Pillow`, `moviepy`, `trimesh`, `aiofiles`) using pip if
  they are not found.

## Installation

1. **Clone or Download:** Place the entire `comfyui_folder_server` directory
   inside your `ComfyUI/custom_nodes/` folder.
   ```bash
   cd ComfyUI/custom_nodes/
   git clone https://github.com/queiul/comfyui_folder_server comfyui_folder_server
   # OR download the ZIP and extract it here
   ```
2. **Install Dependencies:** The addon will attempt to install dependencies
   automatically when ComfyUI starts. However, it's recommended to install them
   manually, especially if you encounter issues or are using a virtual
   environment:
   ```bash
   cd comfyui_folder_server
   pip install -r requirements.txt
   ```
   _(Ensure you are running pip within the Python environment used by ComfyUI)_
3. **Restart ComfyUI:** Completely stop and restart the ComfyUI server.

Check the ComfyUI console output during startup for messages related to "Folder
Server" initialization and dependency checks.

## Usage

Once installed and ComfyUI is running, the Folder Server API is available under
the `/folder_server` base path relative to your ComfyUI server URL.

- **Example API Endpoint:** `http://127.0.0.1:8188/folder_server/list/input`
- **WebSocket Endpoint:** `ws://127.0.0.1:8188/ws/folder_server`

### API Documentation

Detailed documentation for all API endpoints, parameters, responses, WebSocket
messages, and current limitations is available directly via the server:

- **Access the docs:** Navigate to
  `http://{your_comfyui_host}:{port}/folder_server/docs` in your browser (e.g.,
  `http://127.0.0.1:8188/folder_server/docs`).

This `docs.html` file (included in the addon) provides examples and a WebSocket
test console.

## Dependencies

This addon relies on the following Python packages:

- `watchdog>=3.0.0`: For monitoring file system changes.
- `Pillow>=9.0.0`: For image metadata extraction.
- `moviepy>=1.0.3`: For video metadata extraction.
- `trimesh>=3.9.0`: For 3D model metadata extraction (may pull in many
  sub-dependencies like `numpy`, `scipy`).
- `aiofiles>=23.1.0`: For asynchronous file uploads.

The addon attempts to `pip install` missing dependencies. If this fails (e.g.,
due to permissions, network issues, or complex `trimesh` dependencies), you will
need to install them manually using the `requirements.txt` file.

## Current Limitations

- **No Overwrite Option:** The `move` and `copy` API endpoints do not currently
  support overwriting existing files/directories at the destination. They will
  return a `409 Conflict` error instead.
- **Basic WebSocket Events:** Notifications sent via WebSocket only contain
  basic event information (action, path, type, timestamp). Detailed metadata
  (like size or dimensions) is not included to keep events lightweight. Clients
  should re-query the `/list` endpoint if details are needed.
- **Metadata Errors:** Extraction of video/3D metadata might fail for certain
  files or formats, depending on the capabilities and robustness of the
  underlying libraries (`moviepy`, `trimesh`). Errors are typically logged, and
  the metadata fields will be omitted from the API response.
- **Cache Invalidation:** The directory listing cache is invalidated based on
  detected file system events. There's a small theoretical window where rapid
  changes might lead to brief inconsistencies.

## Future Plans

- Add an `overwrite=true` option for `move` and `copy` operations.
- Optionally include more details in WebSocket events.
- Improve error handling and reporting for metadata extraction.
- Add thumbnail generation capabilities.
- Implement archive (e.g., ZIP) creation/extraction endpoints.
- Allow configuration of caching parameters.

## Contributing / Issues

Please report any bugs, issues, or feature requests via the GitHub repository's
[Issues](https://github.com/queiul/comfyui_folder_server/issues) page.
