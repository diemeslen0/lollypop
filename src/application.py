# Copyright (c) 2014-2016 Cedric Bellegarde <cedric.bellegarde@adishatz.org>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
gi.require_version('GstAudio', '1.0')
gi.require_version('GstPbutils', '1.0')
gi.require_version('Notify', '0.7')
gi.require_version('TotemPlParser', '1.0')
from gi.repository import Gtk, Gio, GLib, Gdk, Notify, TotemPlParser

from pickle import dump
from locale import getlocale
from gettext import gettext as _
from threading import Thread


try:
    from lollypop.lastfm import LastFM
except Exception as e:
    print(e)
    print(_("    - Scrobbler disabled\n"
            "    - Auto cover download disabled\n"
            "    - Artist informations disabled"))
    print("$ sudo pip3 install pylast")
    LastFM = None

from lollypop.utils import is_gnome, is_unity, get_network_available
from lollypop.define import Type, DataPath
from lollypop.window import Window
from lollypop.database import Database
from lollypop.player import Player
from lollypop.art import Art
from lollypop.sqlcursor import SqlCursor
from lollypop.settings import Settings, SettingsDialog
from lollypop.database_albums import AlbumsDatabase
from lollypop.database_artists import ArtistsDatabase
from lollypop.database_genres import GenresDatabase
from lollypop.database_tracks import TracksDatabase
from lollypop.playlists import Playlists
from lollypop.objects import Album
from lollypop.collectionscanner import CollectionScanner


class Application(Gtk.Application):
    """
        Lollypop application:
            - Handle appmenu
            - Handle command line
            - Create main window
    """

    def __init__(self):
        """
            Create application
        """
        Gtk.Application.__init__(
                            self,
                            application_id='org.gnome.Lollypop',
                            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        GLib.setenv('PULSE_PROP_media.role', 'music', True)
        GLib.setenv('PULSE_PROP_application.icon_name', 'lollypop', True)
        self.cursors = {}
        self.window = None
        self.notify = None
        self.lastfm = None
        self.debug = False
        self.__externals_count = 0
        self.__init_proxy()
        GLib.set_application_name('Lollypop')
        GLib.set_prgname('lollypop')
        self.add_main_option("album", b'a', GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING, "Play album", None)
        self.add_main_option("debug", b'd', GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Debug lollypop", None)
        self.add_main_option("set-rating", b'r', GLib.OptionFlags.NONE,
                             GLib.OptionArg.INT, "Rate the current track",
                             None)
        self.add_main_option("play-pause", b't', GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Toggle playback",
                             None)
        self.add_main_option("next", b'n', GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Go to next track",
                             None)
        self.add_main_option("prev", b'p', GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Go to prev track",
                             None)
        self.add_main_option("emulate-phone", b'e', GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE,
                             "Emulate an Android Phone",
                             None)
        self.connect('command-line', self.__on_command_line)
        self.connect('activate', self.__on_activate)
        self.register(None)
        if self.get_is_remote():
            Gdk.notify_startup_complete()

    def init(self):
        """
            Init main application
        """
        self.__is_fs = False
        if Gtk.get_minor_version() > 18:
            cssProviderFile = Gio.File.new_for_uri(
                'resource:///org/gnome/Lollypop/application.css')
        else:
            cssProviderFile = Gio.File.new_for_uri(
                'resource:///org/gnome/Lollypop/application-legacy.css')
        cssProvider = Gtk.CssProvider()
        cssProvider.load_from_file(cssProviderFile)
        screen = Gdk.Screen.get_default()
        styleContext = Gtk.StyleContext()
        styleContext.add_provider_for_screen(screen, cssProvider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_USER)
        self.settings = Settings.new()
        self.db = Database()
        self.playlists = Playlists()
        # We store cursors for main thread
        SqlCursor.add(self.db)
        SqlCursor.add(self.playlists)
        self.albums = AlbumsDatabase()
        self.artists = ArtistsDatabase()
        self.genres = GenresDatabase()
        self.tracks = TracksDatabase()
        self.player = Player()
        self.scanner = CollectionScanner()
        self.art = Art()
        self.art.update_art_size()
        if self.settings.get_value('artist-artwork'):
            GLib.timeout_add(5000, self.art.cache_artists_info)
        if LastFM is not None:
            self.lastfm = LastFM()
        if not self.settings.get_value('disable-mpris'):
            # Ubuntu > 16.04
            if Gtk.get_minor_version() > 18:
                from lollypop.mpris import MPRIS
            # Ubuntu <= 16.04, Debian Jessie, ElementaryOS
            else:
                from lollypop.mpris_legacy import MPRIS
            MPRIS(self)
        if not self.settings.get_value('disable-notifications'):
            from lollypop.notification import NotificationManager
            self.notify = NotificationManager()

        settings = Gtk.Settings.get_default()
        dark = self.settings.get_value('dark-ui')
        settings.set_property('gtk-application-prefer-dark-theme', dark)

        self.add_action(self.settings.create_action('playback'))
        self.add_action(self.settings.create_action('shuffle'))

        self.db.upgrade()

    def do_startup(self):
        """
            Init application
        """
        Gtk.Application.do_startup(self)
        Notify.init("Lollypop")

        # Check locale, we want unicode!
        (code, encoding) = getlocale()
        if encoding is None or encoding != "UTF-8":
            builder = Gtk.Builder()
            builder.add_from_resource('/org/gnome/Lollypop/Unicode.ui')
            self.window = builder.get_object('unicode')
            self.window.set_application(self)
            self.window.show()
        elif not self.window:
            self.init()
            menu = self.__setup_app_menu()
            # If GNOME/Unity, add appmenu
            if is_gnome() or is_unity():
                self.set_app_menu(menu)
            self.window = Window()
            # If not GNOME/Unity add menu to toolbar
            if not is_gnome() and not is_unity():
                self.window.setup_menu(menu)
            self.window.connect('delete-event', self.__hide_on_delete)
            self.window.init_list_one()
            self.window.show()
            self.player.restore_state()
            # We add to mainloop as we want to run
            # after player::restore_state() signals
            GLib.idle_add(self.window.toolbar.set_mark)
            # Will not start sooner
            # Ubuntu > 16.04
            if Gtk.get_minor_version() > 18:
                from lollypop.inhibitor import Inhibitor
            # Ubuntu <= 16.04, Debian Jessie, ElementaryOS
            else:
                from lollypop.inhibitor_legacy import Inhibitor
            self.inhibitor = Inhibitor()
            self.charts = None
            if self.settings.get_value('show-charts'):
                if GLib.find_program_in_path("youtube-dl") is not None:
                    from lollypop.charts import Charts
                    self.charts = Charts()
                    if get_network_available():
                        self.charts.update()
                else:
                    self.settings.set_value('network-search',
                                            GLib.Variant('b', False))

    def prepare_to_exit(self, action=None, param=None):
        """
            Save window position and view
        """
        if self.__is_fs:
            return
        if self.settings.get_value('save-state'):
            self.window.save_view_state()
            # Save current track
            if self.player.current_track.id is None:
                track_id = -1
            elif self.player.current_track.id == Type.RADIOS:
                from lollypop.radios import Radios
                radios = Radios()
                track_id = radios.get_id(
                                    self.player.current_track.album_artists[0])
            else:
                track_id = self.player.current_track.id
                # Save albums context
                try:
                    dump(self.player.context.genre_ids,
                         open(DataPath + "/genre_ids.bin", "wb"))
                    dump(self.player.context.artist_ids,
                         open(DataPath + "/artist_ids.bin", "wb"))
                    self.player.shuffle_albums(False)
                    dump(self.player.get_albums(),
                         open(DataPath + "/albums.bin", "wb"))
                except Exception as e:
                    print("Application::prepare_to_exit()", e)
            dump(track_id, open(DataPath + "/track_id.bin", "wb"))
            # Save current playlist
            if self.player.current_track.id == Type.RADIOS:
                playlist_ids = [Type.RADIOS]
            elif not self.player.get_user_playlist_ids():
                playlist_ids = []
            else:
                playlist_ids = self.player.get_user_playlist_ids()
            dump(playlist_ids,
                 open(DataPath + "/playlist_ids.bin", "wb"))
        if self.player.current_track.id is not None:
            position = self.player.position
        else:
            position = 0
        dump(position, open(DataPath + "/position.bin", "wb"))
        self.player.stop_all()
        if self.window:
            self.window.stop_all()
        self.quit()

    def quit(self):
        """
            Quit lollypop
        """
        if self.scanner.is_locked():
            self.scanner.stop()
            GLib.idle_add(self.quit)
            return
        self.db.del_tracks(self.tracks.get_non_persistent())
        try:
            from lollypop.radios import Radios
            with SqlCursor(self.db) as sql:
                sql.execute('VACUUM')
            with SqlCursor(self.playlists) as sql:
                sql.execute('VACUUM')
            with SqlCursor(Radios()) as sql:
                sql.execute('VACUUM')
        except Exception as e:
            print("Application::quit(): ", e)
        self.window.destroy()

    def is_fullscreen(self):
        """
            Return True if application is fullscreen
        """
        return self.__is_fs

    def set_mini(self, action, param):
        """
            Set mini player on/off
            @param dialog as Gtk.Dialog
            @param response id as int
        """
        if self.window is not None:
            self.window.set_mini()

#######################
# PRIVATE             #
#######################
    def __init_proxy(self):
        """
            Init proxy setting env
        """
        try:
            settings = Gio.Settings.new('org.gnome.system.proxy.http')
            h = settings.get_value('host').get_string()
            p = settings.get_value('port').get_int32()
            if h != '' and p != 0:
                GLib.setenv('http_proxy', "%s:%s" % (h, p), True)
                GLib.setenv('https_proxy', "%s:%s" % (h, p), True)
        except:
            pass

    def __on_command_line(self, app, app_cmd_line):
        """
            Handle command line
            @param app as Gio.Application
            @param options as Gio.ApplicationCommandLine
        """
        self.__externals_count = 0
        options = app_cmd_line.get_options_dict()
        if options.contains('debug'):
            self.debug = True
        if options.contains('set-rating'):
            value = options.lookup_value('set-rating').get_int32()
            if value > 0 and value < 6 and\
                    self.player.current_track.id is not None:
                self.player.current_track.set_popularity(value)
        if options.contains('play-pause'):
            self.player.play_pause()
        elif options.contains('album'):
            try:
                value = options.lookup_value('album').get_string()
                album_ids = value.split(';')
                album = Album(int(album_ids.pop(0)))
                self.player.play_album(album)
                for album_id in album_ids:
                    self.player.add_album(Album(int(album_id)))
            except:
                pass
        elif options.contains('next'):
            self.player.next()
        elif options.contains('prev'):
            self.player.prev()
        elif options.contains('emulate-phone'):
            self.window.add_fake_phone()
        args = app_cmd_line.get_arguments()
        if len(args) > 1:
            self.player.clear_externals()
            for uri in args[1:]:
                try:
                    uri = GLib.filename_to_uri(uri)
                except:
                    pass
                parser = TotemPlParser.Parser.new()
                parser.connect('entry-parsed', self.__on_entry_parsed)
                parser.parse_async(uri, True, None, None)
        if self.window is not None and not self.window.is_visible():
            # self.window.setup_window()
            # self.window.present()
            # Horrible HACK: https://bugzilla.gnome.org/show_bug.cgi?id=774130
            self.window.save_view_state()
            self.window.destroy()
            self.window = Window()
            # If not GNOME/Unity add menu to toolbar
            if not is_gnome() and not is_unity():
                menu = self.__setup_app_menu()
                self.window.setup_menu(menu)
            self.window.connect('delete-event', self.__hide_on_delete)
            self.window.init_list_one()
            self.window.show()
            self.player.emit('status-changed')
            self.player.emit('current-changed')
        return 0

    def __on_entry_parsed(self, parser, uri, metadata):
        """
            Add playlist entry to external files
            @param parser as TotemPlParser.Parser
            @param track uri as str
            @param metadata as GLib.HastTable
        """
        self.player.load_external(uri)
        if self.__externals_count == 0:
            if self.player.is_party:
                self.player.set_party(False)
            self.player.play_first_external()
        self.__externals_count += 1

    def __hide_on_delete(self, widget, event):
        """
            Hide window
            @param widget as Gtk.Widget
            @param event as Gdk.Event
        """
        if not self.settings.get_value('background-mode') or\
                self.player.current_track.id is None:
            GLib.timeout_add(500, self.prepare_to_exit)
            self.scanner.stop()
        return widget.hide_on_delete()

    def __update_db(self, action=None, param=None):
        """
            Search for new music
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        if self.window:
            t = Thread(target=self.art.clean_all_cache)
            t.daemon = True
            t.start()
            self.window.update_db()

    def __set_network(self, action, param):
        """
            Enable/disable network
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        action.set_state(param)
        self.settings.set_value('network-access', param)
        if self.charts is not None:
            if param.get_boolean():
                self.charts.update()
            else:
                self.charts.stop()
        self.window.reload_view()

    def __fullscreen(self, action=None, param=None):
        """
            Show a fullscreen window with cover and artist informations
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        if self.window and not self.__is_fs:
            from lollypop.fullscreen import FullScreen
            fs = FullScreen(self, self.window)
            fs.connect("destroy", self.__on_fs_destroyed)
            self.__is_fs = True
            fs.show()

    def __on_fs_destroyed(self, widget):
        """
            Mark fullscreen as False
            @param widget as Fullscreen
        """
        self.__is_fs = False
        if not self.window.is_visible():
            self.prepare_to_exit()

    def __on_activate(self, application):
        """
            Call default handler
            @param application as Gio.Application
        """
        self.window.present()

    def __settings_dialog(self, action=None, param=None):
        """
            Show settings dialog
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        dialog = SettingsDialog()
        dialog.show()

    def __about(self, action, param):
        """
            Setup about dialog
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        builder = Gtk.Builder()
        builder.add_from_resource('/org/gnome/Lollypop/AboutDialog.ui')
        about = builder.get_object('about_dialog')
        about.set_transient_for(self.window)
        about.connect("response", self.__about_response)
        about.show()

    def __shortcuts(self, action, param):
        """
            Show help in yelp
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        try:
            builder = Gtk.Builder()
            builder.add_from_resource('/org/gnome/Lollypop/Shortcuts.ui')
            builder.get_object('shortcuts').set_transient_for(self.window)
            builder.get_object('shortcuts').show()
        except:  # GTK < 3.20
            self.__help(action, param)

    def __help(self, action, param):
        """
            Show help in yelp
            @param action as Gio.SimpleAction
            @param param as GLib.Variant
        """
        try:
            Gtk.show_uri(None, "help:lollypop", Gtk.get_current_event_time())
        except:
            print(_("Lollypop: You need to install yelp."))

    def __about_response(self, dialog, response_id):
        """
            Destroy about dialog when closed
            @param dialog as Gtk.Dialog
            @param response id as int
        """
        dialog.destroy()

    def __setup_app_menu(self):
        """
            Setup application menu
            @return menu as Gio.Menu
        """
        builder = Gtk.Builder()
        builder.add_from_resource('/org/gnome/Lollypop/Appmenu.ui')
        menu = builder.get_object('app-menu')

        settingsAction = Gio.SimpleAction.new('settings', None)
        settingsAction.connect('activate', self.__settings_dialog)
        self.add_action(settingsAction)

        updateAction = Gio.SimpleAction.new('update_db', None)
        updateAction.connect('activate', self.__update_db)
        self.add_action(updateAction)

        networkAction = Gio.SimpleAction.new_stateful(
           'network',
           None,
           GLib.Variant.new_boolean(self.settings.get_value('network-access')))
        networkAction.connect('change-state', self.__set_network)
        self.add_action(networkAction)

        fsAction = Gio.SimpleAction.new('fullscreen', None)
        fsAction.connect('activate', self.__fullscreen)
        self.add_action(fsAction)

        mini_action = Gio.SimpleAction.new('mini', None)
        mini_action.connect('activate', self.set_mini)
        self.add_action(mini_action)

        aboutAction = Gio.SimpleAction.new('about', None)
        aboutAction.connect('activate', self.__about)
        self.add_action(aboutAction)

        shortcutsAction = Gio.SimpleAction.new('shortcuts', None)
        shortcutsAction.connect('activate', self.__shortcuts)
        self.add_action(shortcutsAction)

        helpAction = Gio.SimpleAction.new('help', None)
        helpAction.connect('activate', self.__help)
        self.add_action(helpAction)

        quitAction = Gio.SimpleAction.new('quit', None)
        quitAction.connect('activate', self.prepare_to_exit)
        self.add_action(quitAction)

        return menu
