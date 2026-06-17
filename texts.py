"""Typing passages.

Kept deliberately ASCII-only (no tabs, no newlines, straight quotes) so that
every character maps to a single keystroke on every platform and terminal.
"""

import random

PASSAGES = [
    "The quick brown fox jumps over the lazy dog while the sleepy cat watches "
    "from the warm windowsill without a single care in the whole wide world.",

    "Programming is not about typing fast, it is about thinking clearly, but a "
    "little speed at the keyboard never hurt anyone racing against a friend.",

    "She sells seashells by the seashore, and the shells she sells are surely "
    "seashells, so if she sells shells on the shore then the shells are shore shells.",

    "A journey of a thousand miles begins with a single step, and every expert "
    "was once a beginner who simply refused to quit when the going got tough.",

    "The early bird catches the worm, but the second mouse always gets the "
    "cheese, so timing matters far more than speed in most of life's small races.",

    "Code is read far more often than it is written, so write it for the next "
    "person who has to understand it, because that person might just be you.",

    "In the middle of difficulty lies opportunity, and the people who change "
    "the world are usually the ones who are crazy enough to think they can.",

    "Practice does not make perfect, only perfect practice makes perfect, so "
    "slow down, focus on every single keystroke, and the speed will follow.",

    "The network is reliable, latency is zero, bandwidth is infinite, and the "
    "topology never changes; these are the famous fallacies of computing folklore.",

    "Type the words exactly as they appear on the screen, fix your mistakes "
    "with the backspace key, and try to keep a steady rhythm to the finish line.",

    "Good design is as little design as possible, because less is more when "
    "every extra feature is one more thing that can break on a quiet Friday night.",

    "When in doubt, leave it out; a clean and simple solution that works today "
    "beats a clever and complicated one that might possibly work tomorrow.",

    "The best way to predict the future is to invent it, so stop waiting for "
    "permission and start building the small thing that only you can imagine.",

    "Stay hungry, stay foolish, keep your eyes open for the next big idea, and "
    "never let the fear of looking silly stop you from asking a simple question.",

    "Measure twice and cut once, because a moment of careful planning will save "
    "you an entire afternoon of frustrating rework that nobody ever enjoys doing.",

    "A smooth sea never made a skilled sailor, so welcome the hard problems as "
    "the training ground where ordinary people quietly grow into capable experts.",

    "Keep your friends close, your dependencies closer, and your version numbers "
    "pinned, because the build that worked last week has a funny way of breaking.",

    "Talk is cheap, show me the code, said the engineer who had read one too "
    "many grand proposals that promised the moon and delivered an empty folder.",
]


def pick_text(rng=None, avoid=None):
    """Return a random passage, avoiding ``avoid`` when more than one exists."""
    rng = rng or random
    choices = [p for p in PASSAGES if p != avoid] or PASSAGES
    return rng.choice(choices)
