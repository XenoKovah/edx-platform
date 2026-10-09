/* globals _, gettext, interpolate, ngettext */

/*
 * The "Completion Time" tab: how long learners say that each section of the course takes, calculated by the
 * server from their Timing Feedback answers.
 *
 * The server sends the numbers for both views ("All Responses" and "Complete Responses Only") in one response, so
 * switching between them doesn't ask for anything.  Everything that came from the course or from learners (section
 * and user names) is put on the page as text, never as HTML.
 *
 * Instructors can tick "Exclude Outlier" for learners whose answers would skew the numbers.  Ticking only records
 * the choice; the next Refresh recalculates without those learners, in both views.  The choice is kept in the
 * browser for each course (localStorage), so it also applies to every later visit.
 */

(function($, gettext, _) {
    'use strict';

    var CompletionTime,
        EM_DASH = '\u2014',
        VIEWS = ['all', 'complete'],
        MAX_SECTION_NAMES_SHOWN = 5;

    function element(tagName, className, text) {
        var node = document.createElement(tagName);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = text;
        }
        return node;
    }

    function isNumber(value) {
        return typeof value === 'number' && isFinite(value);
    }

    // Whole minutes as "3h 25m".  Like the oneshot.sh script, this drops the seconds rather than rounding them.
    function formatDuration(minutes) {
        var whole, hours;
        if (!isNumber(minutes)) {
            return EM_DASH;
        }
        whole = Math.floor(minutes);
        hours = Math.floor(whole / 60);
        return interpolate(gettext('%(hours)sh %(minutes)sm'), {hours: hours, minutes: whole - hours * 60}, true);
    }

    function exactMinutes(minutes) {
        if (!isNumber(minutes)) {
            return '';
        }
        return interpolate(gettext('%(minutes)s minutes'), {minutes: (Math.round(minutes * 10) / 10).toString()}, true);
    }

    function durationCell(minutes) {
        var cell = element('td', 'completion-time-number', formatDuration(minutes));
        if (isNumber(minutes)) {
            cell.title = exactMinutes(minutes);
        }
        return cell;
    }

    function countText(count) {
        return interpolate(ngettext('%(count)s learner', '%(count)s learners', count), {count: count}, true);
    }

    CompletionTime = (function() {
        function completionTime($section) {
            var self = this;
            this.$section = $section;
            this.$section.data('wrapper', this);
            this.$container = this.$section.find('.completion-time-container');
            this.endpoint = this.$container.data('endpoint');
            this.$refresh = this.$container.find('.completion-time-refresh');
            this.$status = this.$container.find('.completion-time-status');
            this.$error = this.$container.find('.completion-time-error');
            this.$notes = this.$container.find('.completion-time-notes');
            this.$tabs = this.$container.find('.completion-time-tab');
            this.$panels = this.$container.find('.completion-time-panel');
            this.loaded = false;
            this.loading = false;
            this.storageKey = 'completionTimeExcluded:' + this.endpoint;
            this.excluded = this.readExcluded();

            this.$refresh.click(function() {
                self.load();
            });
            this.$tabs.click(function() {
                self.showView($(this).data('view'));
            });
            this.$tabs.keydown(function(event) {
                self.onTabKeydown(event);
            });
        }

        // The statistics are calculated when the tab is first opened, and again whenever Refresh is pressed.
        completionTime.prototype.onClickTitle = function() {
            if (!this.loaded && !this.loading) {
                this.load();
            }
        };

        completionTime.prototype.load = function() {
            var self = this;
            if (this.loading) {
                return;
            }
            this.loading = true;
            this.$refresh.prop('disabled', true);
            this.$error.prop('hidden', true).empty();
            this.$status.text(gettext('Loading...'));
            $.ajax({
                type: 'GET',
                dataType: 'json',
                url: this.endpoint,
                data: {exclude: this.excludedUsernames()},
                traditional: true,
                cache: false,
                success: function(data) {
                    self.loaded = true;
                    self.render(data);
                },
                error: function(xhr) {
                    self.$status.text('');
                    self.$error.text(interpolate(
                        gettext('The completion time statistics could not be loaded (error %(status)s). ' +
                                'Use Refresh to try again.'),
                        {status: xhr.status},
                        true
                    )).prop('hidden', false);
                },
                complete: function() {
                    self.loading = false;
                    self.$refresh.prop('disabled', false);
                }
            });
        };

        completionTime.prototype.excludedUsernames = function() {
            return _.keys(this.excluded).sort();
        };

        // Browsers can refuse storage (private windows, blocked site data), in which case the choice lasts as long
        // as the page does.
        completionTime.prototype.readExcluded = function() {
            var excluded = {};
            try {
                $.each(JSON.parse(window.localStorage.getItem(this.storageKey) || '[]'), function(index, username) {
                    excluded[username] = true;
                });
            } catch (error) {
                return {};
            }
            return excluded;
        };

        completionTime.prototype.saveExcluded = function() {
            try {
                window.localStorage.setItem(this.storageKey, JSON.stringify(this.excludedUsernames()));
            } catch (error) {
                return;
            }
        };

        // Tick or untick a learner everywhere they are listed.  Nothing is recalculated until Refresh is pressed.
        completionTime.prototype.setExcluded = function(username, exclude) {
            if (exclude) {
                this.excluded[username] = true;
            } else {
                delete this.excluded[username];
            }
            this.saveExcluded();
            this.$container.find('.completion-time-exclude').each(function() {
                if (this.getAttribute('data-username') === username) {
                    this.checked = exclude;
                }
            });
            this.$status.text(gettext('The excluded learners changed. Press Refresh to recalculate.'));
        };

        completionTime.prototype.showView = function(view) {
            this.$tabs.each(function() {
                var $tab = $(this),
                    active = $tab.data('view') === view;
                $tab.toggleClass('active-section', active);
                $tab.attr({'aria-selected': active ? 'true' : 'false', tabindex: active ? '0' : '-1'});
            });
            this.$panels.each(function() {
                $(this).prop('hidden', $(this).data('view') !== view);
            });
        };

        // Arrow keys, Home and End move between the two views, as they do in any set of tabs.
        completionTime.prototype.onTabKeydown = function(event) {
            var last = this.$tabs.length - 1,
                current = this.$tabs.index(event.currentTarget),
                target;
            if (event.key === 'ArrowRight') {
                target = current === last ? 0 : current + 1;
            } else if (event.key === 'ArrowLeft') {
                target = current === 0 ? last : current - 1;
            } else if (event.key === 'Home') {
                target = 0;
            } else if (event.key === 'End') {
                target = last;
            } else {
                return;
            }
            event.preventDefault();
            this.$tabs.eq(target).focus().click();
        };

        completionTime.prototype.render = function(data) {
            var self = this;
            this.renderNotes(data);
            this.$panels.each(function() {
                var $panel = $(this),
                    view = $panel.data('view');
                if (VIEWS.indexOf(view) !== -1 && data.views[view]) {
                    self.renderView($panel.find('.completion-time-results').empty(), data.views[view], view, data);
                }
            });
            this.$status.text(interpolate(
                gettext('Last updated %(time)s'),
                {time: new Date(data.generated_at).toLocaleString()},
                true
            ));
        };

        // Things that the instructors should know about before reading the numbers.
        completionTime.prototype.renderNotes = function(data) {
            var $notes = this.$notes.empty(),
                missing = $.grep(data.sections, function(section) {
                    return !section.has_responses;
                }),
                names;

            if (!data.sections.length) {
                $notes.append(element('p', '', gettext(
                    'No Timing Feedback sections were found in this course. A Timing Feedback section is a ' +
                    'subsection that is named or graded "Timing Feedback" or "Time Collection", and that has a ' +
                    'problem with "Hours:" and "Minutes:" inputs.'
                )));
            } else if (missing.length) {
                names = $.map(missing.slice(0, MAX_SECTION_NAMES_SHOWN), function(section) {
                    return section.label;
                }).join(', ');
                if (missing.length > MAX_SECTION_NAMES_SHOWN) {
                    names += interpolate(
                        gettext(', and %(count)s more'),
                        {count: missing.length - MAX_SECTION_NAMES_SHOWN},
                        true
                    );
                }
                $notes.append(element('p', '', interpolate(
                    ngettext(
                        '%(count)s Timing Feedback section has no responses yet and is not counted: %(names)s',
                        '%(count)s Timing Feedback sections have no responses yet and are not counted: %(names)s',
                        missing.length
                    ),
                    {count: missing.length, names: names},
                    true
                )));
            }
            if (data.excluded && data.excluded.length) {
                $notes.append(element('p', '', interpolate(
                    ngettext(
                        '%(count)s learner is excluded from these numbers: %(names)s',
                        '%(count)s learners are excluded from these numbers: %(names)s',
                        data.excluded.length
                    ),
                    {count: data.excluded.length, names: data.excluded.join(', ')},
                    true
                )));
            }
            if (data.invalid_responses) {
                $notes.append(element('p', '', interpolate(
                    ngettext(
                        '%(count)s answer was ignored because its Hours or Minutes input was left unanswered.',
                        '%(count)s answers were ignored because their Hours or Minutes input was left unanswered.',
                        data.invalid_responses
                    ),
                    {count: data.invalid_responses},
                    true
                )));
            }
            $notes.prop('hidden', !$notes.children().length);
        };

        completionTime.prototype.renderView = function($target, view, viewName, data) {
            var target = $target[0];
            if (!data.sections.length) {
                return;
            }
            if (!view.learners) {
                target.appendChild(element('p', 'completion-time-empty', viewName === 'complete' ?
                    gettext('No learner has answered every Timing Feedback section yet.') :
                    gettext('No learner has answered a Timing Feedback section yet.')));
                return;
            }
            target.appendChild(this.summaryList(view));
            target.appendChild(element('h4', 'hd hd-4', gettext('Time per learner')));
            target.appendChild(element('p', 'completion-time-description', (viewName === 'complete' ?
                gettext('Each learner\'s total is the time that they say they spent on every section.') :
                gettext('Each learner\'s total only adds up the sections that they answered.')) + ' ' +
                gettext('Tick Exclude Outlier to leave a learner out of every number in both views, ' +
                        'then press Refresh.')
            ));
            target.appendChild(this.learnerTable(view.learner_totals));
            target.appendChild(element('h4', 'hd hd-4', gettext('Time per section')));
            target.appendChild(this.sectionTable(view.sections));
        };

        completionTime.prototype.summaryList = function(view) {
            var list = element('dl', 'completion-time-summary'),
                items = [
                    [gettext('Sum of section averages'), formatDuration(view.sum_of_averages), view.sum_of_averages],
                    [gettext('Sum of section medians'), formatDuration(view.sum_of_medians), view.sum_of_medians],
                    [gettext('Average total per learner'), formatDuration(view.average_total), view.average_total],
                    [gettext('Median total per learner'), formatDuration(view.median_total), view.median_total],
                    [gettext('Learners counted'), countText(view.learners), null]
                ];
            $.each(items, function(index, item) {
                var group = element('div', 'completion-time-summary-item'),
                    value = element('dd', '', item[1]);
                if (isNumber(item[2])) {
                    value.title = exactMinutes(item[2]);
                }
                group.appendChild(element('dt', '', item[0]));
                group.appendChild(value);
                list.appendChild(group);
            });
            return list;
        };

        completionTime.prototype.sectionTable = function(sections) {
            var wrapper = element('div', 'completion-time-scroll'),
                table = element('table', 'completion-time-table'),
                head = table.createTHead().insertRow(),
                body = table.createTBody();
            $.each([
                gettext('Section'), gettext('Responses'), gettext('Average'), gettext('Median'),
                gettext('Shortest'), gettext('Longest')
            ], function(index, title) {
                var cell = element('th', index ? 'completion-time-number' : '', title);
                cell.scope = 'col';
                head.appendChild(cell);
            });
            $.each(sections, function(index, section) {
                var row = body.insertRow();
                row.appendChild(element('th', '', section.label)).scope = 'row';
                row.appendChild(element('td', 'completion-time-number', section.count));
                row.appendChild(durationCell(section.average));
                row.appendChild(durationCell(section.median));
                row.appendChild(durationCell(section.min));
                row.appendChild(durationCell(section.max));
                if (!section.count) {
                    row.className = 'completion-time-no-data';
                }
            });
            wrapper.appendChild(table);
            return wrapper;
        };

        // The learners start out with the longest total first, so that outliers are at the top; the headings re-sort.
        completionTime.prototype.learnerTable = function(learners) {
            var self = this,
                wrapper = element('div', 'completion-time-scroll completion-time-scroll-tall'),
                table = element('table', 'completion-time-table'),
                head = table.createTHead().insertRow(),
                body = table.createTBody(),
                rows = learners.slice(),
                sort = {key: 'total', ascending: false},
                columns = [
                    {key: 'username', title: gettext('Learner'), numeric: false},
                    {key: 'sections', title: gettext('Sections answered'), numeric: true},
                    {key: 'total', title: gettext('Total time'), numeric: true}
                ],
                headings = [];

            function fill() {
                rows.sort(function(a, b) {
                    var x = sort.key === 'username' ? a.username.toLowerCase() : a[sort.key],
                        y = sort.key === 'username' ? b.username.toLowerCase() : b[sort.key],
                        order = x < y ? -1 : (x > y ? 1 : 0);
                    return (sort.ascending ? order : -order) || (a.username < b.username ? -1 : 1);
                });
                $(body).empty();
                $.each(rows, function(index, learner) {
                    var row = body.insertRow(),
                        cell = element('td', 'completion-time-exclude-cell'),
                        checkbox = element('input', 'completion-time-exclude');
                    row.appendChild(element('th', '', learner.username)).scope = 'row';
                    row.appendChild(element('td', 'completion-time-number', learner.sections));
                    row.appendChild(durationCell(learner.total)).className += ' completion-time-total';
                    checkbox.type = 'checkbox';
                    checkbox.checked = !!self.excluded[learner.username];
                    checkbox.setAttribute('data-username', learner.username);
                    checkbox.setAttribute('aria-label', interpolate(
                        gettext('Exclude %(username)s'), {username: learner.username}, true
                    ));
                    checkbox.addEventListener('change', function() {
                        self.setExcluded(learner.username, checkbox.checked);
                    });
                    cell.appendChild(checkbox);
                    row.appendChild(cell);
                    if (learner.excluded) {
                        row.className = 'completion-time-excluded';
                    }
                });
                $.each(headings, function(index, heading) {
                    heading.setAttribute(
                        'aria-sort',
                        columns[index].key === sort.key ? (sort.ascending ? 'ascending' : 'descending') : 'none'
                    );
                });
            }

            $.each(columns, function(index, column) {
                var cell = element('th', column.numeric ? 'completion-time-number' : ''),
                    button = element('button', 'completion-time-sort', column.title);
                button.type = 'button';
                button.addEventListener('click', function() {
                    sort = {
                        key: column.key,
                        ascending: sort.key === column.key ? !sort.ascending : column.key === 'username'
                    };
                    fill();
                });
                cell.scope = 'col';
                cell.appendChild(button);
                head.appendChild(cell);
                headings.push(cell);
            });
            head.appendChild(
                element('th', 'completion-time-exclude-cell', gettext('Exclude Outlier'))
            ).scope = 'col';
            fill();
            wrapper.appendChild(table);
            return wrapper;
        };

        return completionTime;
    }());

    _.defaults(window, {
        InstructorDashboard: {}
    });

    _.defaults(window.InstructorDashboard, {
        sections: {}
    });

    _.defaults(window.InstructorDashboard.sections, {
        CompletionTime: CompletionTime
    });
}($, gettext, _));
