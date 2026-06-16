// eslint-disable-next-line no-shadow-restricted-names
(function(undefined) {
    describe('VideoQualityControl', function() {
        var state, qualityControl, videoPlayer, player,
            STORAGE_KEY = 'edx-video-quality-preference';

        function clearStoredQuality() {
            try {
                window.localStorage.removeItem(STORAGE_KEY);
            } catch (e) { } // eslint-disable-line no-empty
        }

        beforeEach(function() {
            clearStoredQuality();
        });

        afterEach(function() {
            $('source').remove();
            clearStoredQuality();
            if (state.storage) {
                state.storage.clear();
            }
            state.videoPlayer.destroy();
        });

        describe('constructor, YouTube mode', function() {
            beforeEach(function() {
                state = jasmine.initializePlayerYouTube();
                qualityControl = state.videoQualityControl;
                videoPlayer = state.videoPlayer;
                player = videoPlayer.player;
            });

            it('renders a (initially hidden) menu-based quality control', function() {
                expect(qualityControl.el).toHaveClass('quality');
                expect(qualityControl.el).toHaveClass('menu-container');
                expect(qualityControl.el).toHaveClass('is-hidden');
                expect(qualityControl.qualityButton).toHaveClass('quality-control');
            });

            it('adds ARIA attributes to the quality control', function() {
                expect(qualityControl.qualityButton).toHaveAttrs({
                    'aria-disabled': 'false',
                    'aria-expanded': 'false'
                });
            });

            it('binds the play, click and hover handlers', function() {
                expect(state.el).toHandle('play');
                expect(qualityControl.el).toHandle('click');
                expect(qualityControl.el).toHandle('mouseenter');
            });

            it('opens and closes the menu', function() {
                videoPlayer.onPlay();
                qualityControl.openMenu();
                expect(qualityControl.el).toHaveClass('is-opened');
                qualityControl.closeMenu();
                expect(qualityControl.el).not.toHaveClass('is-opened');
            });

            it('reads available qualities from YouTube only once', function() {
                expect(player.getAvailableQualityLevels.calls.count()).toEqual(0);

                videoPlayer.onPlay();
                videoPlayer.onPlay();

                expect(player.getAvailableQualityLevels.calls.count()).toEqual(1);
            });

            it('defaults to the highest available quality on first play', function() {
                videoPlayer.onPlay();

                expect(player.setPlaybackQuality).toHaveBeenCalledWith('highres');
                expect(qualityControl.userChoice).toEqual('highres');
            });

            it('reveals the control and builds the menu on play', function() {
                videoPlayer.onPlay();

                expect(qualityControl.el).not.toHaveClass('is-hidden');
                // one option per reported level, plus an Auto entry.
                expect(qualityControl.el.find('.quality-option').length).toEqual(7);
                expect(qualityControl.el.find('li[data-quality="auto"]').length).toEqual(1);
            });

            it('marks the active quality in the menu', function() {
                videoPlayer.onPlay();

                expect(qualityControl.el.find('li[data-quality="highres"]'))
                    .toHaveClass('is-active');
            });

            it('leaves the control hidden when YouTube reports no levels', function() {
                player.getAvailableQualityLevels.and.returnValue([]);

                videoPlayer.onPlay();

                expect(qualityControl.el).toHaveClass('is-hidden');
            });

            it('applies, remembers and logs a quality picked from the menu', function() {
                var requested = jasmine.createSpy('qualitychange:requested');

                videoPlayer.onPlay();
                player.setPlaybackQuality.calls.reset();
                state.el.on('qualitychange:requested', requested);

                qualityControl.el.find('li[data-quality="hd720"] .quality-option').click();

                expect(player.setPlaybackQuality).toHaveBeenCalledWith('hd720');
                expect(qualityControl.userChoice).toEqual('hd720');
                expect(requested).toHaveBeenCalled();
                expect(requested.calls.mostRecent().args[1]).toEqual('hd720');
                expect(window.localStorage.getItem(STORAGE_KEY)).toEqual('hd720');
            });

            it('reverts YouTube to automatic when "Auto" is picked', function() {
                videoPlayer.onPlay();
                player.setPlaybackQuality.calls.reset();

                qualityControl.el.find('li[data-quality="auto"] .quality-option').click();

                expect(player.setPlaybackQuality).toHaveBeenCalledWith('default');
                expect(qualityControl.userChoice).toEqual('auto');
            });

            it('restores a remembered quality on play', function() {
                window.localStorage.setItem(STORAGE_KEY, 'hd720');

                videoPlayer.onPlay();

                expect(player.setPlaybackQuality).toHaveBeenCalledWith('hd720');
                expect(qualityControl.el.find('li[data-quality="hd720"]'))
                    .toHaveClass('is-active');
            });

            it('reflects the actual quality reported by YouTube on the button', function() {
                videoPlayer.onPlay();

                qualityControl.onQualityChange('hd720');

                expect(qualityControl.qualityButton.find('.value').text()).toEqual('720p');
            });

            it('can destroy itself', function() {
                state.videoQualityControl.destroy();
                expect(state.videoQualityControl).toBeUndefined();
                expect($('.quality-control')).not.toExist();
            });
        });

        describe('constructor, HTML5 mode', function() {
            it('does not contain the quality control', function() {
                state = jasmine.initializePlayer();

                expect(state.el.find('.quality-control').length).toBe(0);
            });
        });
    });
}).call(this);
