from schizospider.extractor import extract_from_html


FRAMESET_HTML = """
<html>
  <head><title>jodi snap</title></head>
  <frameset cols="50%,50%">
    <frame src="left.html" name="l">
    <frame src="right.html" name="r">
  </frameset>
</html>
"""

LINKS_HTML = """
<html><body>
  <a href="page1.html">one</a>
  <a href="HTTP://External.COM/Path?b=2&a=1#x">external</a>
  <a href="mailto:foo@bar">mail</a>
  <a href="javascript:window.open('jspopup.html')">popup</a>
  <area href="map.html" alt="m">
  <iframe src="embedded.html" title="t"></iframe>
  <script>
    location.href = 'after-redirect.html';
  </script>
</body></html>
"""


def test_frameset_extracts_both_frames():
    out = extract_from_html(FRAMESET_HTML, "http://example.com/")
    urls = [u for u, _, _ in out]
    assert "http://example.com/left.html" in urls
    assert "http://example.com/right.html" in urls


def test_links_html_mix():
    out = extract_from_html(LINKS_HTML, "http://example.com/")
    urls = [u for u, _, _ in out]
    kinds = {u: k for u, _, k in out}

    assert "http://example.com/page1.html" in urls
    assert "http://external.com/Path?a=1&b=2" in urls  # canonicalized
    # external_scheme links preserved verbatim
    assert "mailto:foo@bar" in urls
    assert kinds["mailto:foo@bar"] == "external_scheme"
    # area link
    assert "http://example.com/map.html" in urls
    # iframe link
    assert "http://example.com/embedded.html" in urls
    # js extracted URLs
    assert "http://example.com/jspopup.html" in urls
    assert "http://example.com/after-redirect.html" in urls


# Real jodi.org pattern: random JS picks one of N forms to document.write,
# and the rendered DOM contains the selected form. We want both the rendered
# action AND every alternate action that appears in the script body.
JODI_FORMS_HTML = """
<html><body>
<script>
if (x > .95) document.write("<form action='w1.html'><input TYPE='submit' value='SELECT'></form>");
if (x > .85) document.write("<form action='w2.html'><input TYPE='submit' value='SELECT'></form>");
if (x > .75) document.write("<form action='w3.html'><input TYPE='submit' value='SELECT'></form>");
</script>
<form action="w8.html"><input type="submit" value="SELECT"></form>
</body></html>
"""


def test_form_action_extracted_from_dom_and_script():
    out = extract_from_html(JODI_FORMS_HTML, "http://example.com/")
    urls = [u for u, _, _ in out]
    # Rendered form's action (in the DOM):
    assert "http://example.com/w8.html" in urls
    # All three alternates from the inline script body:
    assert "http://example.com/w1.html" in urls
    assert "http://example.com/w2.html" in urls
    assert "http://example.com/w3.html" in urls
