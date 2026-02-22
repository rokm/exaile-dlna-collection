#  Copyright (C) 2018 Rok Mandeljc
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#

from gettext import gettext as _
import logging
import weakref

import gi

# Put under if block to avoid triggering E402 warnings in subsequent imports...
if True:
    gi.require_version('GLib', '2.0')
    gi.require_version('GObject', '2.0')
    gi.require_version('GUPnP', '1.6')
    gi.require_version('GUPnPAV', '1.0')
    gi.require_version('Gdk', '3.0')
    gi.require_version('Gtk', '3.0')

from gi.repository import GLib
from gi.repository import GObject
from gi.repository import GUPnP
from gi.repository import GUPnPAV
from gi.repository import Gtk
from gi.repository import Gdk

import xl.collection
import xl.event
import xl.trax
import xl.providers

import xlgui.panel.collection
import xlgui.panel.menus
import xlgui.widgets.menu


logger = logging.getLogger(__name__)


class DlnaCollectionPanel(xlgui.panel.collection.CollectionPanel):
    __gsignals__ = {
        'disconnect-request': (
            GObject.SignalFlags.RUN_LAST,
            None,
            (),
        ),
    }

    def __init__(self, parent, collection):
        super().__init__(
            parent,
            collection,
            collection.udn,
            _show_collection_empty_message=False,
            label=collection.name,
        )

        weak_self = weakref.ref(self)

        # Replace the CollectionPanelMenu with TrackPanelMenu, which
        # does not have actions such as "Open directory" or
        # "Move to trash"
        self.menu = xlgui.panel.menus.TrackPanelMenu(self)

        # Add a "Disconnect" button to the top of the panel
        top_box = self.builder.get_object("collection_top_hbox")

        disconnect_icon = Gtk.Image(stock=Gtk.STOCK_DISCONNECT)

        button = Gtk.Button(image=disconnect_icon)
        button.set_relief(Gtk.ReliefStyle.NONE)  # Be consistent with the rest of panel
        button.set_tooltip_text("Disconnect from share")
        button.connect(
            "button-press-event",
            lambda *args: weak_self().on_disconnect_button_press_event(*args),
        )

        top_box.pack_end(button, False, False, 0)
        button.show()

    def on_refresh_button_press_event(self, button, event):
        """Override the refresh button action."""
        if event.get_state() & Gdk.ModifierType.SHIFT_MASK:
            self.collection.rescan_media_server()
        else:
            self.load_tree()

    def on_disconnect_button_press_event(self, button, event):
        """Disconnect button press handler."""
        GObject.idle_add(self.emit, "disconnect-request")

    def __del__(self):
        logger.debug("DLNA Collection panel destroyed!")


class DlnaCollection(xl.trax.TrackDB):
    def __init__(self, media_server):
        super().__init__(media_server.get_friendly_name())

        self.udn = media_server.get_udn()

        # This is a property from xl.collection.Collection that is
        # expected by the xlgui.panel.collection.CollectionPanel
        self._scanning = False

        # Store reference to media server
        self._media_server = media_server

        # Update when tracks change
        handler_id = self._media_server.connect(
            'tracks-changed',
            self.on_tracks_changed,
        )
        self._tracks_changed_handler = handler_id

        # Connect to server (perform initial update)
        self._media_server.connect_to_server()

    def __del__(self):
        logger.debug("DLNA Collection object destroyed!")

    def shutdown(self):
        # Clean up the signal connection
        self._media_server.disconnect(self._tracks_changed_handler)
        self._tracks_changed_handler = None

        # Clean-up underlying MediaServer object
        self._media_server.disconnect_from_server()
        self._media_server = None

    def on_tracks_changed(self, media_server):
        logger.debug("DLNA Collection: tracks changed!")

        new_tracks = media_server.get_tracks()

        # Threaded
        self.update_tracks(new_tracks)

    def rescan_media_server(self):
        logger.debug("DLNA Collection: rescan media server")
        self._media_server.rescan_audio_items()

    @xl.common.threaded
    def update_tracks(self, new_tracks):
        self._scanning = True

        # Remove old tracks; this is a bit roundabout, but works...
        old_tracks = self.get_tracks()
        self.remove_tracks(old_tracks)

        # Add new tracks
        self.add_tracks(new_tracks)

        self._scanning = False


class MediaServer(GUPnP.DeviceProxy):
    _CONTENT_DIR = "urn:schemas-upnp-org:service:ContentDirectory"
    _MAX_REQUEST_SIZE = 4096

    __gsignals__ = {
        'tracks-changed': (
            GObject.SignalFlags.RUN_LAST,
            None,
            (),
        )
    }

    def __init__(self):
        super().__init__()

        self._content_directory = None
        self._scanning = False
        self._tracks = []

    def __del__(self):
        logger.debug(
            "%r: %r %r destroyed!",
            self,
            self.get_udn(),
            self.get_friendly_name(),
        )

    def get_tracks(self):
        return self._tracks

    def connect_to_server(self):
        # Get server's content directory
        self._content_directory = self.get_service(self._CONTENT_DIR)

        # Subscribe to update notifications
        weak_self = weakref.ref(self)
        self._content_directory.add_notify(
            "SystemUpdateID",
            str,
            lambda *args: weak_self().on_system_update_id(*args),
        )
        self._content_directory.set_subscribed(True)

        self._last_update_id = None
        self._update_timeout_id = None

        # Initial update
        self.rescan_audio_items()

    def disconnect_from_server(self):
        # Clear content directory
        self._content_directory.set_subscribed(False)
        self._content_directory = None

    def on_system_update_id(self, content_directory, variable, value):
        """Called whenever the contents of the media server change."""

        logger.debug("%r: system updated IDs!", self)

        # Ignore initial ID update
        if self._last_update_id is None:
            logger.debug("%r: initial ID update - ignoring!", self)
            self._last_update_id = value
            return

        # Require the update ID to differ from the previous one
        if self._last_update_id == value:
            logger.debug("%r: ID update, but no change in ID - ignoring!", self)
            return

        self._last_update_id = value

        # Schedule refresh; according to spec, the system update ID
        # event is moderated at maximum rate of 0.5 Hz (once every
        # two seconds). So we wait 5 seconds before running the update
        if self._update_timeout_id is not None:
            GLib.source_remove(self._update_timeout_id)

        weak_self = weakref.ref(self)
        self._update_timeout_id = GLib.timeout_add_seconds(
            5,
            lambda *args: weak_self().on_system_update_id_timeout(),
        )

    def on_system_update_id_timeout(self):
        """Called 5 seconds after last on_system_update_id() call."""

        logger.debug("%r: update timeout - starting rescan!", self)

        self._update_timeout_id = None

        # Threaded! Will emit a signal when scan is complete.
        self.rescan_audio_items()

        return False

    def identify_server(self):
        """Identifies the media server type in order to enable metadata quirks."""

        #model_name = self.get_model_name()
        model_description = self.get_model_description()  # Might be None!

        if "MiniDLNA" in (model_description or ""):
            return "minidlna"

        return "generic"

    @xl.common.threaded
    def rescan_audio_items(self):
        logger.debug("%r: scanning media server for audio items!", self)

        if self._scanning:
            logger.debug("%r: scan already in progress!", self)
            return

        # Get server name and description to enable quirks
        server_type = self.identify_server()

        self._scanning = True

        # Issue a search - we can use the synchronous variant, because
        # we are being called in a thread anyway...
        start_index = 0
        request_size = self._MAX_REQUEST_SIZE

        # DIDL parsing
        all_tracks = []

        def on_didl_object_available(parser, didl_object):
            """Called when DIDL-Lite parser parses a DIDL object"""

            # Process only audio items
            if not didl_object.get_upnp_class().startswith('object.item.audioItem'):
                return

            # Create track with primary URI
            resources = didl_object.get_resources()

            resource = resources[0]  # FIXME: find best resource?

            uri = resource.get_uri()
            track = xl.trax.Track(uri, scan=False)

            try:
                track.set_tag_raw(
                    '__length',
                    resource.get_duration(),
                    notify_changed=False,
                )
            except Exception:
                track.set_tag_raw(
                    '__length',
                    0,
                    notify_changed=False,
                )

            # *** Set up metadata ***
            # Artist, album artist, composer: depends on the server
            composer = None
            artist = None
            album_artist = None

            if server_type == "minidlna":
                # MiniDLNA returns both Artist (as "creator") and Album
                # artist (as "artist" without role)
                artist = didl_object.get_creator()
                album_artist = didl_object.get_artist()
            else:
                # Generic codepath; attempt to retrieve artist and
                # album artist from list of contributors. If not
                # available, fall-back to the "creator"
                for entry in didl_object.get_artists():
                    role = entry.get_role()
                    if role is None:
                        artist = entry.get_name()
                    elif role == "AlbumArtist":
                        album_artist = entry.get_name()

                for entry in didl_object.get_authors():
                    role = entry.get_role()
                    if role == "Composer":
                        composer = entry.get_name()

                # Fall back to creator
                if artist is None:
                    artist = didl_object.get_creator()

            if artist is not None:
                track.set_tag_raw('artist', [artist], notify_changed=False)

            if album_artist is not None:
                track.set_tag_raw('albumartist', [album_artist], notify_changed=False)

            if composer is not None:
                track.set_tag_raw('composer', [composer], notify_changed=False)

            # Title
            title = didl_object.get_title()
            if title is not None:
                track.set_tag_raw('title', [title], notify_changed=False)

            # Album
            album = didl_object.get_album()
            if album is not None:
                track.set_tag_raw('album', [album], notify_changed=False)

            # Track number
            track_number = didl_object.get_track_number()
            if track_number is not None:
                track.set_tag_raw('tracknumber', [str(track_number)], notify_changed=False)

            # Track year
            date = didl_object.get_date()
            if date is not None:
                tokens = date.split('-')
                track.set_tag_raw('date', [tokens[0]], notify_changed=False)

            # Append to list
            all_tracks.append(track)

        # Parser
        parser = GUPnPAV.DIDLLiteParser.new()
        parser.connect("object-available", on_didl_object_available)

        # Process
        while True:
            # Search from start index
            action = GUPnP.ServiceProxyAction.new_from_list(
                "Search",
                ('ContainerID', 'SearchCriteria', 'Filter', 'StartingIndex', 'RequestedCount', 'SortCriteria'),
                ('0', 'upnp:class derivedfrom "object.item.audioItem"', '*', start_index, request_size, ''),
            )

            self._content_directory.call_action(action)  # TODO: handle error

            (status, out_values) = action.get_result_list(
                ('Result', 'NumberReturned', 'TotalMatches'),
                (str, int, int),
            )

            del action

            didl_xml = out_values[0]
            number_returned = out_values[1]
            total_matches = out_values[2]

            logger.debug('Retreieved %d music items!' % (number_returned))

            # Parse the returned DIDL
            if number_returned > 0:
                parser.parse_didl(didl_xml)

            # Do we have to retrieve more?
            # NOTE: some implementations (e.g., rygel) return 0 total
            # matches; in such cases, we try again until we receive zero
            # matches.
            start_index = start_index + number_returned
            remaining = total_matches - start_index

            if (remaining > 0 or total_matches == 0) and number_returned != 0:
                if remaining > 0:
                    request_size = min(remaining, self._MAX_REQUEST_SIZE)
                else:
                    request_size = self._MAX_REQUEST_SIZE
            else:
                logger.debug("%r: retrieved all music items!", self)
                break

        # Cleanup
        self._scanning = False

        # Set the tracks
        logger.debug("%r: retrieved %d audio tracks!", len(all_tracks))

        self._tracks = all_tracks

        #self.emit("tracks-changed")
        GObject.idle_add(self.emit, "tracks-changed")


GObject.type_register(MediaServer)


class DlnaManager(GObject.GObject):
    __gsignals__ = {
        'connect-to-server': (
            GObject.SignalFlags.RUN_LAST,
            None,
            (GObject.TYPE_STRING, ),
        ),
    }

    def __init__(self, exaile, menu):
        super().__init__()

        self._context_manager = None

        self._exaile = exaile

        self._media_servers = {}
        self._panels = {}

        # Menu UI
        self._menu = menu

        weak_self = weakref.ref(self)  # Create a weak reference to pass to the item's callback

        item = xlgui.widgets.menu.simple_menu_item(
            'rescan',
            [],
            _('Rescan...'),
            callback=lambda *args: weak_self().rescan(),
        )
        self._menu.add_item(item)

        item = xlgui.widgets.menu.simple_separator('sep', ['rescan'])
        self._menu.add_item(item)

        # Register MediaServer class with GUPnP resource factory
        # This way, device-proxy-available signal will create an instance
        # of MediaServer instead of base GUPnP.DeviceProxy
        factory = GUPnP.ResourceFactory.get_default()

        factory.register_resource_proxy_type("urn:schemas-upnp-org:device:MediaServer:1", MediaServer)
        factory.register_resource_proxy_type("urn:schemas-upnp-org:device:MediaServer:2", MediaServer)
        factory.register_resource_proxy_type("urn:schemas-upnp-org:device:MediaServer:3", MediaServer)
        factory.register_resource_proxy_type("urn:schemas-upnp-org:device:MediaServer:4", MediaServer)

        # Create GUPnP context manager
        self._context_manager = GUPnP.ContextManager.create(0)
        self._context_manager.connect("context-available", weak_self().on_context_available)
        self._context_manager.rescan_control_points()

        self.connect('connect-to-server', lambda o, u: weak_self().on_connect_to_server(u))

    def __del__(self):
        """Overriden to track object's lifetime."""
        logger.debug("%r: destroyed!", self)

    def shutdown(self):
        self._exaile = None
        self._context_manager = None

        #self._media_servers = {}

    def on_context_available(self, context_manager, context):
        """Called when GUPnP context becomes available."""

        weak_self = weakref.ref(self)  # Create a weak reference to self to pass on to signal connections

        # Create control point that monitors appearance and disappearance
        # of media servers
        control_point = GUPnP.ControlPoint.new(context, "urn:schemas-upnp-org:device:MediaServer:1")
        control_point.connect(
            "device-proxy-available",
            lambda *args: weak_self().on_server_proxy_available(*args),
        )
        control_point.connect(
            "device-proxy-unavailable",
            lambda *args: weak_self().on_server_proxy_unavailable(*args),
        )

        control_point.set_active(True)

        # Let context manager manage the control point
        context_manager.manage_control_point(control_point)

    def on_server_proxy_available(self, control_point, media_server):
        """Called when a Media Server becomes available."""
        udn = media_server.get_udn()
        friendly_name = media_server.get_friendly_name()

        logger.debug("DLNA Media Server available: %r, %r", udn, friendly_name)

        # Store the media server
        if udn in self._media_servers:
            logger.debug("Server with this UDN already in the list! Ignoring...")
            return

        logger.debug("Adding server to the list!")
        self._media_servers[udn] = media_server

        # Rebuild menu items list
        self.rebuild_server_menu_items()

    def on_server_proxy_unavailable(self, control_point, media_server):
        """Called when a Media Server becomes unavailable."""
        udn = media_server.get_udn()

        logger.debug("DLNA Media Server became unavailable: %r", udn)

        # Clean-up the panel
        if udn in self._panels:
            panel = self._panels[udn]

            panel.collection.shutdown()
            #panel.collection = None

            # Remove provider
            xl.providers.unregister('main-panel', panel)

            # Remove reference
            del self._panels[udn]

        # Remove the reference to media server proxy
        if udn in self._media_servers:
            del self._media_servers[udn]

        self.rebuild_server_menu_items()

    def on_disconnect_request(self, panel):
        """Called when user requests disconnect from the panel."""

        logger.debug("Disconnect from share requested by user!")

        udn = panel.collection.udn

        # Shutdown the underlying collection
        panel.collection.shutdown()
        #panel.collection = None

        # Unregister the panel
        xl.providers.unregister('main-panel', panel)

        # Remove from the list
        if udn in self._panels:
            del self._panels[udn]

    def rescan(self, *args):
        """Rescan for DLNA media servers."""

        logger.debug("Manual rescan!")

        # Trigger rescan of control points
        if self._context_manager is not None:
            self._context_manager.rescan_control_points()

    def rebuild_server_menu_items(self):
        """Rebuilds the list of menu items for available servers."""

        # Clear all
        self.clear_menu_items()

        # Build server list...
        servers = []

        for udn, media_server in self._media_servers.items():
            friendly_name = media_server.get_friendly_name()
            servers.append((friendly_name, udn))

        # ... and sort it by friendly name
        #servers = sorted(servers, key=lambda server: server[0].lower())

        # Add items
        for friendly_name, udn in servers:
            self.new_server_menu_item(friendly_name, udn)

    def clear_menu_items(self):
        """Removes all menu items."""

        if self._menu:
            for item in self._menu._items:
                if item.name == 'rescan' or item.name == 'sep':
                    continue
                self._menu.remove_item(item)

    def new_server_menu_item(self, name, udn):
        """Adds a new server menu item."""

        weak_self = weakref.ref(self)

        logger.debug("Adding menu %s: %s", name, udn)
        if self._menu:
            menu_item = xlgui.widgets.menu.simple_menu_item(
                udn,
                ['sep'],
                name,
                callback=lambda *args: weak_self().on_server_menu_entry_clicked(udn),
            )
            self._menu.add_item(menu_item)

    def on_server_menu_entry_clicked(self, udn):
        """Called when user clicks on a server menu item."""

        logger.debug("Clicked on server entry: %r", udn)

        GObject.idle_add(self.emit, "connect-to-server", udn)

    def on_connect_to_server(self, udn):
        """Connection request"""

        if udn in self._panels:
            logger.debug("Panel already opened!")

            # A somewhat hackish way to ensure that the panel is reopend
            # if the user closed it without disconnecting
            panel = self._panels[udn]
            xl.providers.unregister('main-panel', panel)
            xl.providers.register('main-panel', panel)
            return

        # Create collection object
        collection = DlnaCollection(self._media_servers[udn])

        # Create new panel
        weak_self = weakref.ref(self)

        panel = DlnaCollectionPanel(self._exaile.gui.main.window, collection)
        panel.connect(
            "disconnect-request",
            lambda *args: weak_self().on_disconnect_request(*args),
        )

        xl.providers.register('main-panel', panel)
        self._panels[udn] = panel


class DlnaCollectionPlugin:
    """The exaile plugin."""

    def __init__(self):
        self._exaile = None
        self._manager = None

    def enable(self, exaile):
        """Enable plugin."""

        self._exaile = exaile

    def teardown(self, exaile):
        """Shutdown plugin."""

        if self._manager is not None:
            self._manager.shutdown()
            self._manager = None

        self._exaile = None

    def disable(self, exaile):
        """Disable plugin."""
        self.teardown(exaile)

        # Remove the menu item
        for item in xl.providers.get('menubar-tools-menu'):
            if item.name == 'dlna':
                xl.providers.unregister('menubar-tools-menu', item)
                break

    def on_gui_loaded(self):
        """GUI setup."""

        # Add menu item
        menu = xlgui.widgets.menu.Menu(None)

        item = xlgui.widgets.menu.simple_separator('plugin-sep', ['track-properties'])
        xl.providers.register('menubar-tools-menu', item)

        item = xlgui.widgets.menu.simple_menu_item(
            'dlna',
            ['plugin-sep'],
            _('Connect to DLNA...'),
            submenu=menu,
        )
        xl.providers.register('menubar-tools-menu', item)

        # Create the UPnP manager
        self._manager = DlnaManager(self._exaile, menu)


plugin_class = DlnaCollectionPlugin
