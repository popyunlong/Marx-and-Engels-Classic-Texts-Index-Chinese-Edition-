from ai_comprehensive import complete_blocks


def test_control_prefix_followed_by_blank_lines_stays_private():
    # Unlike a token split between network chunks, this model output inserts a
    # paragraph break INSIDE the private token. It must never become a paragraph.
    raw = 'Visible paragraph.\n\n<section_\n\n'
    ready, pending = complete_blocks(raw)
    assert ready == 'Visible paragraph.\n\n'
    assert pending == '<section_\n\n'
    assert complete_blocks(pending, final=True) == ('', pending)
