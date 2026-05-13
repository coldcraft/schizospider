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
