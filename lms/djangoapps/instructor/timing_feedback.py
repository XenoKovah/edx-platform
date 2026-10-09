"""
Course completion-time statistics, calculated from the answers to "Timing Feedback" problems.

Most OST2 courses end every section with a Timing Feedback subsection.  It holds a problem with two
dropdowns, "Hours:" and "Minutes:", asking how long the learner spent on that section.  This module

* finds those subsections automatically (``find_timing_sections``), so nobody has to list their names and
  block ids by hand, and
* turns the learners' answers into per-section and per-learner time statistics (``build_report``).

``build_report`` calculates what ``oneshot.sh`` and ``oneshot-i.sh`` (``TimingStatsCalculator_v3.py``) print:

* the "all responses" view includes every learner who answered at least one section, even if they skipped
  others (``oneshot-i.sh``);
* the "complete responses" view only includes learners who answered every section (``oneshot.sh``).

Nothing here touches Django, the modulestore or the database, so it can be tested on its own.  The view in
``lms/djangoapps/instructor/views/completion_time.py`` supplies the course tree and the learners' answers.
"""
import html
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

# A subsection is a Timing Feedback candidate if its name or its graded format (the "assignment type") says so.
# The names in use are "Timing Feedback 01: ...", "Time Collection - ...", "Timing Collection 01 - ...", and
# "<section> Timing Feedback"; the formats are "TimingFeedback" and "Timing Feedback".
_TIMING_NAME_RE = re.compile(r'timing\s*(?:feedback|collection)|time\s*collection', re.IGNORECASE)
_TIMING_FORMAT_RE = re.compile(r'timing|time\s*collection', re.IGNORECASE)

# What gets removed from a subsection name to leave the name of the section that it asks about.
_STOPWATCH_EMOJI_RE = re.compile('\u23f1\ufe0f?')
_LABEL_SEPARATORS = ' \t\u00a0-\u2013\u2014:|.'

_LABEL_ELEMENT_RE = re.compile(r'<label\b[^>]*>(.*?)</label>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')

# Input ids look like "<problem id>_<response number>_<input number>", e.g. "5f4a6020fc53f4885942_3_1".
_INPUT_ID_RE = re.compile(r'_(\d+)_(\d+)$')


@dataclass(frozen=True)
class TimingSection:
    """
    One Timing Feedback problem and the part of the course that it asks about.
    """
    key: str  # usage key of the problem, as a string
    label: str  # the section's name, for display
    sequential: str  # the display name of the Timing Feedback subsection that holds the problem
    chapter: str  # the display name of the chapter that holds that subsection
    hours_first: bool = True  # False if the problem asks for the minutes before the hours


def is_timing_candidate(display_name, graded_format):
    """
    Whether a subsection's name or graded format marks it as a Timing Feedback subsection.
    """
    return bool(_TIMING_NAME_RE.search(display_name or '') or _TIMING_FORMAT_RE.search(graded_format or ''))


def hours_first_in_problem(problem_xml):
    """
    Tell whether a problem is a Timing Feedback problem, i.e. one with an "Hours:" and a "Minutes:" input.

    Returns None if it isn't, otherwise True if it asks for the hours first and False if it asks for the
    minutes first.
    """
    labels = []
    for label in _LABEL_ELEMENT_RE.findall(problem_xml or ''):
        text = html.unescape(_TAG_RE.sub('', label)).replace('\u00a0', ' ')
        labels.append(text.strip().rstrip(':').strip().lower())
    try:
        return labels.index('hours') < labels.index('minutes')
    except ValueError:
        return None


def section_label(subsection_name, chapter_name=''):
    """
    The name of the section that a Timing Feedback subsection asks about.

    "Timing Feedback 00 - CPU Fundamentals" is about "00 - CPU Fundamentals" (with or without a stopwatch emoji in
    front), "Time Collection - SBO" is about "SBO" and "Logic Analyzer Setup Timing Feedback" is about "Logic
    Analyzer Setup".  A subsection that has nothing but "Timing Feedback" in its name is labeled with the name of
    its chapter.
    """
    name = _STOPWATCH_EMOJI_RE.sub('', subsection_name or '')
    match = _TIMING_NAME_RE.search(name)
    if match:
        # The words after "Timing Feedback" say what it is about; if there aren't any, the words before it do.
        label = name[match.end():].strip(_LABEL_SEPARATORS) or name[:match.start()].strip(_LABEL_SEPARATORS)
    else:
        # Only the graded format marks this one as Timing Feedback, so the whole name is the label.
        label = name.strip()
    return label or (chapter_name or '').strip() or (subsection_name or '').strip()


def _descendants(block):
    """
    Yield every block below this one, in course order.
    """
    for child in block.get_children():
        yield child
        yield from _descendants(child)


def find_timing_sections(course):
    """
    Find the Timing Feedback sections of a course, in course order.

    A section is a problem with "Hours:" and "Minutes:" inputs inside a subsection (sequential) that is named
    or graded as Timing Feedback.  Only blocks that can be reached from the course are considered, so orphaned
    blocks are never counted.  `course` is the course block; anything with the same ``get_children()``,
    ``category``, ``display_name_with_default``, ``format``, ``data`` and ``location`` attributes works.
    """
    candidates = []
    for chapter in course.get_children():
        for sequential in chapter.get_children():
            name = sequential.display_name_with_default
            if not is_timing_candidate(name, getattr(sequential, 'format', None)):
                continue
            for block in _descendants(sequential):
                if block.category != 'problem':
                    continue
                hours_first = hours_first_in_problem(getattr(block, 'data', ''))
                if hours_first is not None:
                    candidates.append((
                        str(block.location),
                        section_label(name, chapter.display_name_with_default),
                        name,
                        chapter.display_name_with_default,
                        hours_first,
                        str(sequential.location),
                    ))

    # Different subsections can be named alike ("Beta-tester Time Collection" in every chapter), in which case the
    # chapter says what each one is about.  Whatever is still repeated after that gets numbered.
    subsections = {(label, subsection) for _, label, _, _, _, subsection in candidates}
    subsections_by_label = Counter(label for label, _ in subsections)
    seen = defaultdict(int)
    sections = []
    for key, label, sequential, chapter, hours_first, _ in candidates:
        if subsections_by_label[label] > 1 and chapter.strip():
            label = chapter.strip()
        seen[label] += 1
        if seen[label] > 1:
            label = f'{label} ({seen[label]})'
        sections.append(TimingSection(key, label, sequential, chapter, hours_first))
    return sections


def extract_time_answers(student_answers, hours_first=True):
    """
    Get the raw (hours, minutes) answers out of a StudentModule's ``student_answers``.

    Returns None if there are fewer than two answers.

    The answers are ordered by their input ids, not by the order that they were stored in: the order that the
    LMS stores them in varies from learner to learner, so the first stored answer is sometimes the minutes.
    """
    if not isinstance(student_answers, dict) or len(student_answers) < 2:
        return None

    def input_order(input_id):
        match = _INPUT_ID_RE.search(input_id)
        if match:
            return (0, int(match.group(1)), int(match.group(2)), input_id)
        return (1, len(input_id), 0, input_id)

    first, second = (student_answers[input_id] for input_id in sorted(student_answers, key=input_order)[:2])
    return (first, second) if hours_first else (second, first)


def to_minutes(hours, minutes):
    """
    Convert raw hours and minutes answers to a number of minutes, or None if they aren't whole numbers.

    A dropdown that the learner left alone is stored as something like "option_<id>_3_1_dummy_default".
    """
    try:
        return int(minutes) + 60 * int(hours)
    except (TypeError, ValueError):
        return None


def _stats(values):
    """
    The count, average, median, minimum and maximum of a list of minutes.
    """
    if not values:
        return {'count': 0, 'average': None, 'median': None, 'min': None, 'max': None}
    return {
        'count': len(values),
        'average': sum(values) / len(values),
        'median': statistics.median(values),
        'min': min(values),
        'max': max(values),
    }


def _summarize(sections, minutes_by_learner, include_incomplete, sections_with_responses):
    """
    Calculate one view of the report.

    `minutes_by_learner` maps each username to a dict of {section key: minutes}.  A learner is only counted if
    they answered every section that anyone answered, unless `include_incomplete` is set.
    """
    included = {
        username: answered for username, answered in minutes_by_learner.items()
        if include_incomplete or len(answered) == sections_with_responses
    }
    totals = {username: sum(answered.values()) for username, answered in included.items()}

    section_rows = []
    for section in sections:
        stats = _stats([answered[section.key] for answered in included.values() if section.key in answered])
        section_rows.append({'id': section.key, 'label': section.label, **stats})

    average_minutes = [row['average'] for row in section_rows if row['count']]
    median_minutes = [row['median'] for row in section_rows if row['count']]
    total_stats = _stats(list(totals.values()))
    return {
        'learners': len(included),
        # What a learner needs in total, from the whole class's per-section averages and medians.
        'sum_of_averages': math.fsum(average_minutes) if average_minutes else None,
        'sum_of_medians': math.fsum(median_minutes) if median_minutes else None,
        # What the learners' own totals add up to.
        'average_total': total_stats['average'],
        'median_total': total_stats['median'],
        'sections': section_rows,
        'learner_totals': [
            {'username': username, 'sections': len(included[username]), 'total': totals[username]}
            for username in sorted(totals, key=lambda username: (totals[username], username))
        ],
    }


def build_report(sections, answer_rows):
    """
    Calculate the completion time statistics of a course.

    `sections` is what ``find_timing_sections`` returns.  `answer_rows` has one ``(username, section key,
    student_answers)`` for every learner's attempt at a Timing Feedback problem that has ``student_answers``.

    Returns a dict with the two views that the dashboard offers: ``views['all']`` includes learners who
    skipped some sections, ``views['complete']`` doesn't.  Answers that aren't whole numbers are ignored, and
    counted in ``invalid_responses``.
    """
    sections_by_key = {section.key: section for section in sections}
    sections_with_responses = set()
    minutes_by_learner = defaultdict(dict)
    invalid_responses = 0

    for username, key, student_answers in answer_rows:
        section = sections_by_key.get(key)
        if section is None:
            continue
        sections_with_responses.add(key)
        answers = extract_time_answers(student_answers, section.hours_first)
        if answers is None:
            continue
        hours, minutes = answers
        if hours is None or minutes is None or (not hours and not minutes):
            continue
        total = to_minutes(hours, minutes)
        if total is None:
            invalid_responses += 1
            continue
        minutes_by_learner[username][key] = total

    return {
        'sections': [
            {
                'id': section.key,
                'label': section.label,
                'sequential': section.sequential,
                'chapter': section.chapter,
                'has_responses': section.key in sections_with_responses,
            }
            for section in sections
        ],
        'invalid_responses': invalid_responses,
        'views': {
            'all': _summarize(sections, minutes_by_learner, True, len(sections_with_responses)),
            'complete': _summarize(sections, minutes_by_learner, False, len(sections_with_responses)),
        },
    }
