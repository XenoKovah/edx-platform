// eslint-disable-next-line no-shadow-restricted-names
(function(undefined) {
    // VideoQualityControl is intentionally a no-op (see the module source):
    // YouTube ignores setPlaybackQuality(), so no quality control is rendered.
    describe('VideoQualityControl (disabled)', function() {
        var state;

        afterEach(function() {
            $('source').remove();
            if (state && state.storage) {
                state.storage.clear();
            }
            if (state && state.videoPlayer) {
                state.videoPlayer.destroy();
            }
        });

        it('renders no quality control for a YouTube video', function() {
            state = jasmine.initializePlayerYouTube();

            expect(state.el.find('.quality-control').length).toBe(0);
            expect(state.videoQualityControl).toBeUndefined();
        });

        it('renders no quality control for an HTML5 video', function() {
            state = jasmine.initializePlayer();

            expect(state.el.find('.quality-control').length).toBe(0);
        });
    });
}).call(this);
