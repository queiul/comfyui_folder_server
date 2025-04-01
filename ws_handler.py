# comfyui_folder_server/ws_handler.py

import asyncio
import json
import logging
import weakref
import time # Added
from typing import Set, Dict

from aiohttp import web
from aiohttp.web import Request, WebSocketResponse

logger = logging.getLogger(__name__)

class WebSocketHandler:
    def __init__(self):
        self._clients: weakref.WeakSet[WebSocketResponse] = weakref.WeakSet()
        self._subscriptions: Dict[WebSocketResponse, Set[str]] = weakref.WeakKeyDictionary()
        logger.info("WebSocketHandler initialized")

    async def handle_connection(self, request: Request):
        """Handles incoming WebSocket connections."""
        ws = WebSocketResponse()
        await ws.prepare(request)
        logger.info(f"WebSocket client connected: {request.remote}")
        self._clients.add(ws)
        self._subscriptions[ws] = set()

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        action = data.get("action")
                        folder_type = data.get("folder_type")

                        if action == "subscribe" and folder_type in ["input", "output"]:
                            self._subscriptions.setdefault(ws, set()).add(folder_type) # Ensure ws key exists
                            logger.info(f"Client {request.remote} subscribed to {folder_type}")
                            # Send success confirmation matching user's example
                            await ws.send_json({"action": "subscribe_success", "folder_type": folder_type})
                        elif action == "unsubscribe" and folder_type in ["input", "output"]:
                             subs = self._subscriptions.get(ws, set())
                             if folder_type in subs:
                                subs.remove(folder_type)
                                logger.info(f"Client {request.remote} unsubscribed from {folder_type}")
                                # Send success confirmation matching user's example
                                await ws.send_json({"action": "unsubscribe_success", "folder_type": folder_type})
                             # else: # Optionally notify if unsubscribe is for non-subscribed folder
                             #      await ws.send_json({"status": "not_subscribed", "folder": folder_type})
                        else:
                            logger.warning(f"Received unknown WebSocket action or invalid folder_type: {data}")
                            await ws.send_json({"error": "Unknown action or invalid folder_type", "received": data})

                    except json.JSONDecodeError:
                        logger.error("Received invalid JSON over WebSocket")
                        await ws.send_json({"error": "Invalid JSON"})
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}", exc_info=True)
                        await ws.send_json({"error": str(e)})

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket connection closed with exception {ws.exception()}")

        except Exception as e:
             logger.error(f"Unhandled exception in WebSocket handler for {request.remote}: {e}", exc_info=True)
        finally:
            logger.info(f"WebSocket client disconnected: {request.remote}")
            # Clean up handled by weakref/dictionary automatically
            if ws in self._subscriptions:
                 try:
                     del self._subscriptions[ws]
                 except KeyError:
                     pass # Already gone

        return ws

    async def broadcast(self, event_data: dict):
        """Sends event data to relevant subscribed clients."""
        # Add metadata fetching here? Or keep events simple?
        # Let's keep events simple for now. Clients can query /list if they need full details.
        # If we add metadata, do it carefully to avoid blocking.
        # Example: Fetch metadata async only if there are subscribers for the event type/folder.

        folder_type = event_data.get("folder_type") # Primary folder type from event
        if not folder_type:
            # Try destination folder type if primary is missing (e.g., move event)
            folder_type = event_data.get("destination_folder_type")
            if not folder_type:
                logger.warning("Broadcast attempted without folder_type in event data")
                return

        try:
            message = json.dumps(event_data)
        except Exception as e:
             logger.error(f"Failed to serialize event data for broadcast: {e} - Data: {event_data}")
             return

        # Create a list of tasks to send messages concurrently
        tasks = []
        # Iterate over a copy of clients to avoid issues with disconnections during iteration
        clients_to_notify = list(self._subscriptions.items())

        for ws, subscriptions in clients_to_notify:
            # Check if client is subscribed to the relevant folder type
            if ws in self._subscriptions and folder_type in subscriptions: # Double check ws exists
                if not ws.closed:
                    # Create a task for each send operation
                    tasks.append(self._send_message(ws, message))
                else:
                     # Proactive cleanup if we detect a closed socket here
                     logger.debug("Removing closed WebSocket found during broadcast prep.")
                     if ws in self._subscriptions:
                          try: del self._subscriptions[ws]
                          except KeyError: pass

        # Run all send tasks concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True) # return_exceptions=True logs errors without stopping others

    async def _send_message(self, ws: WebSocketResponse, message: str):
        """Helper task to send a message to a single WebSocket client."""
        try:
            await ws.send_str(message)
        except ConnectionResetError:
             logger.warning(f"ConnectionResetError sending to client. Removing.")
             if ws in self._subscriptions:
                 try: del self._subscriptions[ws]
                 except KeyError: pass
        except Exception as e:
            logger.error(f"Error sending message to WebSocket client: {e}")
            # Remove potentially broken client on other errors too
            if ws in self._subscriptions:
                 try: del self._subscriptions[ws]
                 except KeyError: pass


# Global instance
ws_handler_instance = WebSocketHandler()
