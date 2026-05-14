from schizospider.urls import (
    canonicalize,
    classify,
    extract_js_urls,
    extract_meta_refresh,
    registrable_domain,
    same_site,
)


def test_canonicalize_basic():
    c = canonicalize("HTTP://WWW.Jodi.ORG/?b=2&a=1#frag")
    assert c == "http://www.jodi.org/?a=1&b=2"


def test_canonicalize_resolves_relative():
    c = canonicalize("about/index.html", base="https://wwwwwwwww.jodi.org/oss/")
    assert c == "https://wwwwwwwww.jodi.org/oss/about/index.html"


def test_canonicalize_normalizes_dot_segments():
    c = canonicalize("/a/b/../c/./d", base="http://example.com/")
    assert c == "http://example.com/a/c/d"


def test_canonicalize_rejects_fragment_only():
    assert canonicalize("#top") is None


def test_canonicalize_rejects_non_http():
    assert canonicalize("mailto:foo@bar") is None
    assert canonicalize("javascript:void(0)") is None


def test_classify():
    assert classify("https://foo/") == "http"
    assert classify("/relative") == "http"
    assert classify("mailto:x@y") == "external_scheme"
    assert classify("javascript:alert(1)") == "external_scheme"
    assert classify("data:text/plain,hi") == "external_scheme"
    assert classify("") == "invalid"
    assert classify("#frag") == "invalid"


def test_registrable_domain():
    assert registrable_domain("https://wwwwwwwww.jodi.org/index.html") == "jodi.org"
    assert registrable_domain("https://oss.jodi.org/") == "jodi.org"
    assert registrable_domain("https://www.cnn.com/x") == "cnn.com"


def test_same_site_registrable():
    a = "https://wwwwwwwww.jodi.org/a"
    b = "https://oss.jodi.org/b"
    c = "https://cnn.com/x"
    assert same_site(a, b, "registrable")
    assert not same_site(a, b, "strict")
    assert not same_site(a, c, "registrable")


def test_extract_js_urls():
    out = extract_js_urls("window.open('foo.html')")
    assert out == ["foo.html"]
    out = extract_js_urls("location.href='https://target/'")
    assert out == ["https://target/"]
    out = extract_js_urls("javascript:window.open('a.html');location.replace('b.html')")
    assert "a.html" in out and "b.html" in out


def test_extract_js_urls_picks_up_form_action_in_script_body():
    """jodi.org uses document.write to emit one of N <form action='wN.html'>
    elements at random — we want to find ALL the alternates so the crawler
    doesn't have to depend on which one the random pick chose."""
    js = """
    if (x > .95) document.write("<form action='w1.html'><input TYPE='submit'></form>");
    if (x > .85) document.write("<form action='w2.html'><input TYPE='submit'></form>");
    if (x > .75) document.write('<form action="w3.html"><input TYPE="submit"></form>');
    """
    out = extract_js_urls(js)
    assert "w1.html" in out
    assert "w2.html" in out
    assert "w3.html" in out


def test_extract_meta_refresh():
    assert extract_meta_refresh("5; url=foo.html") == "foo.html"
    assert extract_meta_refresh("0;URL='bar.html'") == "bar.html"
    assert extract_meta_refresh("no url here") is None
