(function(requirejs, require, define) {
// VideoQualityControl module.

    'use strict';

    // This module is intentionally a no-op: it renders no quality control.
    //
    // YouTube's IFrame Player API deprecated playback-quality control.
    // setPlaybackQuality() is ignored, and YouTube selects the quality from the
    // player's rendered pixel size (verified on dev: a 710px player is served
    // 720p, while enlarging the same player to 1920px makes YouTube serve 2160p
    // within seconds -- with no API call). A quality selector, or the legacy
    // "HD" toggle this replaced, therefore cannot actually change quality and
    // only misleads viewers. Until videos are served through a player we can
    // control (a wider embed, or self-hosted HLS/DASH), no quality control is
    // rendered.
    //
    // Deliberately kept elsewhere:
    //   - load-time player sizing in 03_video_player.js (sizes the YT iframe to
    //     its container so YouTube's initial pick matches the displayed size);
    //   - the quality analytics hook in 09_events_plugin.js (the `qualitychange`
    //     event still fires `edx.video.quality.changed` for the quality changes
    //     YouTube makes on its own, e.g. on resize/fullscreen).

    define(
        'video/05_video_quality_control.js',
        [],
        function() {
            // Returns a constructor-shaped function so the module loader in
            // 01_initialize.js/10_main.js can invoke it like any other module;
            // it simply does nothing and adds no control to the UI.
            // eslint-disable-next-line no-unused-vars
            return function(state) { };
        });
}(RequireJS.requirejs, RequireJS.require, RequireJS.define));
