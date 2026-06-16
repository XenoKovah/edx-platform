(function(requirejs, require, define) {
// VideoQualityControl module.

    'use strict';

    define(
        'video/05_video_quality_control.js', [
            'video/00_iterator.js',
            'edx-ui-toolkit/js/utils/html-utils'
        ],
        function(Iterator, HtmlUtils) {
            // Known YouTube quality levels, ordered lowest -> highest, with the
            // friendly labels shown in the menu and on the control button.
            var QUALITY_ORDER = [
                    'tiny', 'small', 'medium', 'large',
                    'hd720', 'hd1080', 'hd1440', 'hd2160', 'highres'
                ],
                QUALITY_LABELS = {
                    tiny: '144p',
                    small: '240p',
                    medium: '360p',
                    large: '480p',
                    hd720: '720p',
                    hd1080: '1080p',
                    hd1440: '1440p',
                    hd2160: '2160p',
                    highres: gettext('High')
                },
                // Our menu value for "let YouTube choose"; 'default' is YouTube's
                // own sentinel for automatic quality.
                AUTO = 'auto',
                YT_AUTO = 'default',
                // Persist the viewer's choice per-browser. The player's built-in
                // VideoStorage (00_video_storage.js) is in-memory only and does
                // not survive a page reload, so use localStorage directly.
                STORAGE_KEY = 'edx-video-quality-preference';

            function readStoredQuality() {
                try {
                    return window.localStorage.getItem(STORAGE_KEY) || null;
                } catch (e) {
                    // localStorage can be unavailable (private mode, or blocked in
                    // a cross-origin iframe). Degrade to "no stored preference".
                    return null;
                }
            }

            function writeStoredQuality(value) {
                try {
                    window.localStorage.setItem(STORAGE_KEY, value);
                } catch (e) {
                    // Preference simply won't persist; not fatal.
                    return false;
                }
                return true;
            }

            /**
             * VideoQualityControl module.
             * @exports video/05_video_quality_control.js
             * @constructor
             * @param {object} state The object containing the state of the video player.
             * @return {jquery Promise}
             */
            var QualityControl = function(state) {
                // Changing quality only applies to YouTube videos.
                if (state.videoType !== 'youtube') {
                    return undefined;
                }

                if (!(this instanceof QualityControl)) {
                    return new QualityControl(state);
                }

                // bindAll makes these methods own (enumerable) properties bound to
                // this instance. That is required for two reasons: the player calls
                // `state.trigger('videoQualityControl.onQualityChange', ...)`, whose
                // dispatcher walks the chain with hasOwnProperty and re-applies the
                // state as `this`; binding makes the method reachable and keeps
                // `this` pointing at the control instance.
                _.bindAll(this,
                    'destroy', 'fetchAvailableQualities', 'onQualityChange',
                    'showQualityControl', 'setQuality', 'markActive',
                    'openMenu', 'closeMenu', 'clickMenuHandler', 'keyDownMenuHandler',
                    'clickLinkHandler', 'keyDownLinkHandler',
                    'mouseEnterHandler', 'mouseLeaveHandler'
                );
                this.state = state;
                this.state.videoQualityControl = this;
                // The viewer's chosen mode: a concrete level string, or AUTO.
                this.userChoice = null;
                // Levels YouTube reports as available, ordered highest -> lowest.
                this.availableQualities = [];
                this.initialize();

                return $.Deferred().resolve().promise();
            };

            QualityControl.prototype = {
                template: [
                    '<div class="quality menu-container is-hidden" role="application">',
                    '<p class="sr instructions">',
                    gettext('Press UP to enter the quality menu then use the UP and DOWN arrow keys to navigate the different quality levels, then press ENTER to change to the selected quality level.'), // eslint-disable-line max-len, indent
                    '</p>',
                    '<button class="control quality-control quality-button" aria-disabled="false" aria-expanded="false" title="', // eslint-disable-line max-len, indent
                    gettext('Adjust video quality'),
                    '">',
                    '<span class="icon icon-hd" aria-hidden="true">HD</span>',
                    '<span class="sr text-translation">',
                    gettext('Video quality'),
                    '</span>',
                    '<span class="value"></span>',
                    '</button>',
                    '<ol class="video-qualities menu"></ol>',
                    '</div>'
                ].join(''),

                destroy: function() {
                    this.el.off({
                        mouseenter: this.mouseEnterHandler,
                        mouseleave: this.mouseLeaveHandler,
                        click: this.openMenu,
                        keydown: this.keyDownMenuHandler
                    });
                    this.state.el.off('.quality');
                    this.closeMenu(true);
                    this.el.remove();
                    delete this.state.videoQualityControl;
                },

                /** Initializes the module. */
                initialize: function() {
                    var state = this.state,
                        instructionsId = 'quality-instructions-' + state.id;

                    this.el = $(this.template);
                    this.qualitiesContainer = this.el.find('.video-qualities');
                    this.qualityButton = this.el.find('.quality-button');

                    HtmlUtils.append(
                        state.el.find('.secondary-controls'),
                        HtmlUtils.HTML(this.el)
                    );

                    // Set a dynamic id on the instructions to avoid collisions when
                    // several videos share a page.
                    this.el.find('.instructions').attr('id', instructionsId);
                    this.qualityButton.attr('aria-describedby', instructionsId);

                    this.bindHandlers();

                    return true;
                },

                /**
                 * Bind any necessary function callbacks to DOM events (click,
                 * mousemove, etc.).
                 */
                bindHandlers: function() {
                    this.el.on({
                        mouseenter: this.mouseEnterHandler,
                        mouseleave: this.mouseLeaveHandler,
                        click: this.openMenu,
                        keydown: this.keyDownMenuHandler
                    });

                    this.qualitiesContainer.on({
                        click: this.clickLinkHandler,
                        keydown: this.keyDownLinkHandler
                    }, '.quality-option');

                    // YouTube only reports the available quality levels once
                    // playback has started, so defer reading them until first play.
                    this.state.el.on('play.quality', _.once(this.fetchAvailableQualities));
                    this.state.el.on('destroy.quality', this.destroy);
                },

                /**
                 * Read the available levels from YouTube (first play only), build
                 * the menu, reveal the control, and apply the initial quality.
                 * @desc Possible YouTube values are 'highres', 'hd2160', 'hd1440',
                 *       'hd1080', 'hd720', 'large', 'medium', 'small', 'tiny'.
                 */
                fetchAvailableQualities: function() {
                    var levels = this.state.videoPlayer.player.getAvailableQualityLevels() || [],
                        stored = readStoredQuality(),
                        initial;

                    // Keep only levels we know how to label, ordered high -> low.
                    this.availableQualities = QUALITY_ORDER.filter(function(level) {
                        return _.contains(levels, level);
                    }).reverse();

                    // Graceful fallback: if YouTube reports nothing usable (it can
                    // return an empty list, especially very early), leave the
                    // control hidden rather than show an empty menu. This mirrors
                    // the old HD button, which only appeared when levels were known.
                    if (!this.availableQualities.length) {
                        return;
                    }

                    this.renderMenu();
                    this.showQualityControl();

                    // Default to the remembered choice when it is still valid,
                    // otherwise to the highest level (legibility first).
                    if (stored === AUTO || _.contains(this.availableQualities, stored)) {
                        initial = stored;
                    } else {
                        initial = this.availableQualities[0]; // highest (high -> low)
                    }

                    // silent: this is the default, not an explicit user action, so
                    // do not (re)persist it or emit a "requested" analytics event.
                    this.setQuality(initial, true);
                },

                /** Build the <li> menu items: levels high -> low, then an Auto entry. */
                renderMenu: function() {
                    var self = this,
                        items = _.map(this.availableQualities, function(level) {
                            return self.optionHtml(level, self.labelFor(level));
                        });

                    items.push(this.optionHtml(AUTO, gettext('Auto')));

                    HtmlUtils.setHtml(this.qualitiesContainer, HtmlUtils.HTML(items.join('')));
                    this.qualityLinks = new Iterator(this.qualitiesContainer.find('.quality-option'));
                },

                optionHtml: function(value, label) {
                    return HtmlUtils.interpolateHtml(
                        HtmlUtils.HTML([
                            '<li data-quality="{value}">',
                            '<button class="control quality-option" tabindex="-1" aria-pressed="false">',
                            '{label}',
                            '</button>',
                            '</li>'
                        ].join('')),
                        {value: value, label: label}
                    ).toString();
                },

                labelFor: function(level) {
                    if (level === AUTO) {
                        return gettext('Auto');
                    }
                    return QUALITY_LABELS[level] || level;
                },

                /**
                 * Shows quality control. Only called once HD/quality levels are
                 * known to be available.
                 */
                showQualityControl: function() {
                    this.el.removeClass('is-hidden');
                },

                /**
                 * Apply a quality choice. `quality` is a concrete level string or
                 * AUTO. setPlaybackQuality is best-effort (modern YouTube may ignore
                 * it), so update the UI optimistically and, for explicit user
                 * choices, persist the preference and log the request.
                 * @param {string} quality
                 * @param {boolean} [silent] true for the initial/default apply.
                 */
                setQuality: function(quality, silent) {
                    this.userChoice = quality;
                    this.markActive(quality);
                    this.qualityButton.find('.value').text(this.labelFor(quality));
                    this.qualityButton.attr(
                        'title',
                        gettext('Video quality: ') + this.labelFor(quality)
                    );

                    this.state.trigger(
                        'videoPlayer.handlePlaybackQualityChange',
                        quality === AUTO ? YT_AUTO : quality
                    );

                    if (!silent) {
                        writeStoredQuality(quality);
                        // Capture the viewer's intent even when YouTube ignores the
                        // request (in which case onPlaybackQualityChange -- and thus
                        // the "changed" analytics event -- may never fire).
                        this.state.el.trigger('qualitychange:requested', [quality]);
                    }
                },

                /** Highlight the menu item for the chosen mode (level or Auto). */
                markActive: function(quality) {
                    this.qualitiesContainer.find('li').each(function(index, el) {
                        var $el = $(el),
                            isActive = $el.data('quality') === quality;
                        $el.toggleClass('is-active', isActive)
                            .find('.quality-option')
                            .attr('aria-pressed', isActive ? 'true' : 'false');
                    });
                },

                /**
                 * Called by the player (via state.trigger) when YouTube reports the
                 * quality it actually switched to. Reflect the real level on the
                 * button so the viewer can see what they are getting, but keep the
                 * menu's active item on the chosen *mode* -- e.g. "Auto" stays
                 * selected even as YouTube varies the real level underneath it.
                 * @param {string} value YouTube quality string.
                 */
                onQualityChange: function(value) {
                    if (!this.availableQualities.length) {
                        return;
                    }
                    if (value && value !== YT_AUTO && value !== AUTO) {
                        this.qualityButton.find('.value').text(this.labelFor(value));
                    }
                },

                // -------------------------------------------------------------
                // Menu open/close + keyboard/mouse handling. Mirrors
                // video/08_video_speed_control.js so the quality menu behaves
                // identically to the speed menu, including accessibility.
                // -------------------------------------------------------------

                /**
                 * Opens quality menu.
                 * @param {boolean} [bindEvent] Click event will be attached on window.
                 */
                openMenu: function(bindEvent) {
                    if (bindEvent) {
                        $(window).on('click.qualityMenu', this.clickMenuHandler);
                    }

                    this.el.addClass('is-opened');
                    this.qualityButton
                        .attr('tabindex', -1)
                        .attr('aria-expanded', 'true');
                },

                /**
                 * Closes quality menu.
                 * @param {boolean} [unBindEvent] Click event will be detached from window.
                 */
                closeMenu: function(unBindEvent) {
                    if (unBindEvent) {
                        $(window).off('click.qualityMenu');
                    }

                    this.el.removeClass('is-opened');
                    this.qualityButton
                        .attr('tabindex', 0)
                        .attr('aria-expanded', 'false');
                },

                clickMenuHandler: function() {
                    this.closeMenu();

                    return false;
                },

                clickLinkHandler: function(event) {
                    var quality = $(event.currentTarget).parent().data('quality');

                    this.setQuality(quality);
                    this.closeMenu(true);

                    return false;
                },

                mouseEnterHandler: function() {
                    this.openMenu();

                    return false;
                },

                mouseLeaveHandler: function() {
                    // Only close the menu if no quality entry has focus.
                    if (!this.qualityLinks.list.is(':focus')) {
                        this.closeMenu();
                    }

                    return false;
                },

                keyDownMenuHandler: function(event) {
                    var KEY = $.ui.keyCode,
                        keyCode = event.keyCode;

                    // eslint-disable-next-line default-case
                    switch (keyCode) {
                    // Open menu and focus on last element of list above it.
                    case KEY.ENTER:
                    case KEY.SPACE:
                    case KEY.UP:
                        this.openMenu(true);
                        this.qualityLinks.last().focus();
                        break;
                        // Close menu.
                    case KEY.ESCAPE:
                        this.closeMenu(true);
                        break;
                    }
                    // We do not stop propagation and default behavior on a TAB
                    // keypress.
                    return event.keyCode === KEY.TAB;
                },

                keyDownLinkHandler: function(event) {
                    // ALT key is used to change (alternate) the function of other
                    // pressed keys. In this case, do nothing.
                    if (event.altKey) {
                        return true;
                    }

                    var KEY = $.ui.keyCode,
                        self = this,
                        parent = $(event.currentTarget).parent(),
                        index = parent.index(),
                        quality = parent.data('quality');

                    // eslint-disable-next-line default-case
                    switch (event.keyCode) {
                    // Close menu.
                    case KEY.TAB:
                    // Closes menu after 25ms delay to change `tabindex` after
                    // finishing default behavior.
                        setTimeout(function() {
                            self.closeMenu(true);
                        }, 25);

                        return true;
                        // Close menu and give focus to quality control.
                    case KEY.ESCAPE:
                        this.closeMenu(true);
                        this.qualityButton.focus();

                        return false;
                        // Scroll up menu, wrapping at the top. Keep menu open.
                    case KEY.UP:
                    // Shift + Arrows keyboard shortcut might be used by
                    // screen readers. In this case, do nothing.
                        if (event.shiftKey) {
                            return true;
                        }

                        this.qualityLinks.prev(index).focus();
                        return false;
                        // Scroll down menu, wrapping at the bottom. Keep menu open.
                    case KEY.DOWN:
                    // Shift + Arrows keyboard shortcut might be used by
                    // screen readers. In this case, do nothing.
                        if (event.shiftKey) {
                            return true;
                        }

                        this.qualityLinks.next(index).focus();
                        return false;
                        // Close menu, give focus to quality control and change quality.
                    case KEY.ENTER:
                    case KEY.SPACE:
                        this.closeMenu(true);
                        this.qualityButton.focus();
                        this.setQuality(quality);

                        return false;
                    }

                    return true;
                }
            };

            return QualityControl;
        });
}(RequireJS.requirejs, RequireJS.require, RequireJS.define));
