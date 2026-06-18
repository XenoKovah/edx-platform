(function(define) {
    'use strict';

    define('video/10_commands.js', [], function() {
        var VideoCommands, Command, playCommand, pauseCommand, togglePlaybackCommand,
            toggleMuteCommand, toggleFullScreenCommand, setSpeedCommand, skipCommand,
            seekForwardCommand, seekBackwardCommand, skipSeek,
            // Number of seconds the left/right arrow keys jump. Both the YouTube
            // and HTML5 players accept an arbitrary seek target, so this is purely
            // a UX choice; kept in sync with the progress slider's arrow-key step.
            SKIP_SECONDS = 5;
        /**
     * Video commands module.
     * @exports video/10_commands.js
     * @constructor
     * @param {Object} state The object containing the state of the video
     * @param {Object} i18n The object containing strings with translations.
     * @return {jquery Promise}
     */
        VideoCommands = function(state, i18n) {
            if (!(this instanceof VideoCommands)) {
                return new VideoCommands(state, i18n);
            }

            _.bindAll(this, 'destroy');
            this.state = state;
            this.state.videoCommands = this;
            this.i18n = i18n;
            this.commands = [];
            this.initialize();

            return $.Deferred().resolve().promise();
        };

        VideoCommands.prototype = {
            destroy: function() {
                this.state.el.off('destroy', this.destroy);
                delete this.state.videoCommands;
            },

            /** Initializes the module. */
            initialize: function() {
                this.commands = this.getCommands();
                this.state.el.on('destroy', this.destroy);
            },

            execute: function(command) {
                var args = [].slice.call(arguments, 1) || [];

                if (_.has(this.commands, command)) {
                    this.commands[command].execute.apply(this, [this.state].concat(args));
                } else {
                    console.log('Command "' + command + '" is not available.');
                }
            },

            getCommands: function() {
                var commands = {},
                    commandsList = [
                        playCommand, pauseCommand, togglePlaybackCommand,
                        toggleMuteCommand, toggleFullScreenCommand, setSpeedCommand,
                        skipCommand, seekForwardCommand, seekBackwardCommand
                    ];

                _.each(commandsList, function(command) {
                    commands[command.name] = command;
                }, this);

                return commands;
            }
        };

        Command = function(name, execute) {
            this.name = name;
            this.execute = execute;
        };

        playCommand = new Command('play', function(state) {
            state.videoPlayer.play();
        });

        pauseCommand = new Command('pause', function(state) {
            state.videoPlayer.pause();
        });

        togglePlaybackCommand = new Command('togglePlayback', function(state) {
            if (state.videoPlayer.isPlaying()) {
                pauseCommand.execute(state);
            } else {
                playCommand.execute(state);
            }
        });

        toggleMuteCommand = new Command('toggleMute', function(state) {
            state.videoVolumeControl.toggleMute();
        });

        toggleFullScreenCommand = new Command('toggleFullScreen', function(state) {
            state.videoFullScreen.toggle();
        });

        setSpeedCommand = new Command('speed', function(state, speed) {
            state.videoSpeedControl.setSpeed(state.speedToString(speed));
        });

        skipCommand = new Command('skip', function(state, doNotShowAgain) {
            if (doNotShowAgain) {
                state.videoBumper.skipAndDoNotShowAgain();
            } else {
                state.videoBumper.skip();
            }
        });

        // Jump the playhead by `delta` seconds (negative to rewind), clamped to
        // the bounds of the video. Used by the left/right arrow-key commands.
        skipSeek = function(state, delta) {
            var videoPlayer = state.videoPlayer,
                duration, oldTime, newTime;

            // The player may not be constructed yet (e.g. an arrow key pressed
            // before the video metadata has loaded). Bail out quietly.
            if (!videoPlayer || !videoPlayer.player) {
                return;
            }

            duration = videoPlayer.duration();
            oldTime = videoPlayer.currentTime;
            if (!_.isFinite(oldTime)) {
                oldTime = 0;
            }
            newTime = Math.max(0, Math.min(oldTime + delta, duration));

            // Already at the start/end -- nothing to seek to.
            if (newTime === oldTime) {
                return;
            }

            // Mirror VideoPlayer.onSeek(): cancel any pending jump back to the
            // configured start time, perform the seek, and emit the analytics
            // `seek` event. We intentionally call seekTo() directly rather than
            // the debounced onSlideSeek(), so that holding an arrow key
            // accumulates correctly (seekTo() updates currentTime synchronously).
            videoPlayer.goToStartTime = false;
            videoPlayer.seekTo(newTime);
            state.el.trigger('seek', [newTime, oldTime, 'onSkipSeek']);
        };

        seekForwardCommand = new Command('seekForward', function(state) {
            skipSeek(state, SKIP_SECONDS);
        });

        seekBackwardCommand = new Command('seekBackward', function(state) {
            skipSeek(state, -SKIP_SECONDS);
        });

        return VideoCommands;
    });
}(RequireJS.define));
