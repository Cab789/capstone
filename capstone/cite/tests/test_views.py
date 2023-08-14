import datetime
import re
import json
from datetime import timedelta
from difflib import unified_diff
from pathlib import Path
from urllib.parse import urlencode

import mock
import pytest
from bs4 import BeautifulSoup

from django.utils import timezone

from capapi.tests.helpers import check_response, is_cached
from capdb.tasks import update_elasticsearch_from_queue, CaseAnalysis
from capweb.helpers import reverse
from test_data.test_fixtures.helpers import set_case_text


@pytest.mark.django_db(databases=['capdb'])
def test_home(client, django_assert_num_queries, reporter):
    """ Test / """
    with django_assert_num_queries(select=2):
        response = client.get(reverse('cite_home', host='cite'))
    check_response(response, content_includes=reporter.full_name)


@pytest.mark.django_db(databases=['capdb'])
def test_series(client, django_assert_num_queries, volume_metadata_factory):
    """ Test /series/ """

    # make sure we correctly handle multiple reporters with same slug
    volume_1, volume_2 = [volume_metadata_factory(
        reporter__short_name='Mass.',
        reporter__short_name_slug='mass',
    ) for _ in range(2)]
    response = client.get(reverse('series', args=['mass'], host='cite'))
    check_response(response)
    content = response.content.decode()
    for vol in (volume_1, volume_2):
        assert vol.volume_number in content
        assert vol.reporter.full_name in content

    # make sure we redirect if series is not slugified
    response = client.get(reverse('series', args=['Mass.'], host='cite'))
    check_response(response, status_code=302)
    response = client.get(reverse('series', args=['mass'], host='cite'), follow=True)
    check_response(response, status_code=200)
    
    # make sure we get 404 if bad series input
    response = client.get(reverse('series', args=['*'], host='cite'))
    check_response(response, status_code=404)


@pytest.mark.django_db(databases=['capdb'])
def test_series_as_citation(client):
    # if series looks like a full case citation, redirect to case
    response = client.get(reverse('series', args=['1 Mass. 1'], host='cite'))
    check_response(response, redirect_to=reverse('citation', args=['mass', '1', '1'], host='cite'))
    # if series looks like a full statutory citation, redirect to statute page
    response = client.get(reverse('series', args=['11 U.S.C. § 550'], host='cite'))
    check_response(response, redirect_to=reverse('citations', host='cite') + '?' + urlencode({'q': '11 U.S.C. § 550'}))


@pytest.mark.django_db(databases=['capdb'])
def test_volume(client, django_assert_num_queries, case_factory, elasticsearch):
    """ Test /series/volume/ """
    cases = [case_factory(
        volume__reporter__full_name='Massachusetts%s' % i,
        volume__reporter__short_name='Mass.',
        volume__reporter__short_name_slug='mass',
        volume__volume_number='1',
        volume__volume_number_slug='1',
    ) for i in range(3)]

    with django_assert_num_queries(select=1):
        response = client.get(reverse('volume', args=['mass', '1'], host='cite'))
    check_response(response)

    content = response.content.decode()
    for case in cases:
        assert case.reporter.full_name in content
        assert case.citations.first().cite in content

    # make sure we redirect if reporter name / series is not slugified
    response = client.get(reverse('volume', args=['Mass.', '1'], host='cite'))
    check_response(response, status_code=302)
    response = client.get(reverse('volume', args=['Mass.', '1'], host='cite'), follow=True)
    check_response(response, status_code=200)

    # make sure we get 404 if bad volume input
    response = client.get(reverse('volume', args=['Mass.', '*'], host='cite'))
    check_response(response, status_code=404)


@pytest.mark.django_db(databases=['capdb'])
def test_case_not_found(client, django_assert_num_queries, elasticsearch):
    """ Test /series/volume/case/ not found """
    with django_assert_num_queries(select=1):
        response = client.get(reverse('citation', args=['fake', '123', '456'], host='cite'))
    check_response(response, content_includes='Search for "123 Fake 456" in other databases')


@pytest.mark.django_db(databases=['capdb'])
def test_cases_multiple(client, django_assert_num_queries, case_factory, elasticsearch):
    """ Test /series/volume/case/ with multiple matching cases """
    cases = [case_factory(
        jurisdiction__whitelisted=True,
        citations__type='official',
        citations__cite='23 Ill. App. 19',
        citations__normalized_cite='23illapp19'
    ) for i in range(3)]
    first_case = cases[0]

    # disambiguation page should work even if cases wrongly end up with same frontend_url
    assert set(c.frontend_url for c in cases) == {'/ill-app/23/19/'}

    # disambiguation page includes all case short names
    check_response(
        client.get(reverse('citation', args=['ill-app', '23', '19'], host='cite'), follow=True),
        content_includes=['Multiple cases match']+[c.name_abbreviation for c in cases],
        content_excludes=first_case.name,
    )

    # single case pages work with ID appended, even if not matching frontend_url
    check_response(
        client.get(reverse('citation', args=['ill-app', '23', '19', first_case.id], host='cite')),
        content_includes=first_case.name,
    )


@pytest.mark.django_db(databases=['default', 'capdb', 'user_data'])
@pytest.mark.parametrize('response_type', ['html', 'pdf'])
def test_single_case(client, auth_client, token_auth_client, case_factory, elasticsearch, response_type, django_assert_num_queries, settings):
    """ Test /series/volume/case/ with one matching case """

    # set up for viewing html or pdf
    case_text = "Case HTML"
    unrestricted_case = case_factory(jurisdiction__whitelisted=True, body_cache__html=case_text, first_page_order=2, last_page_order=2)
    restricted_case = case_factory(jurisdiction__whitelisted=False, body_cache__html=case_text, first_page_order=2, last_page_order=2)
    if response_type == 'pdf':
        case_text = "REMEMBERED"
        unrestricted_url = unrestricted_case.get_pdf_url()
        url = restricted_case.get_pdf_url()
        content_type = 'application/pdf'
    else:
        unrestricted_url = unrestricted_case.get_full_frontend_url()
        url = restricted_case.get_full_frontend_url()
        content_type = None

    ### can load whitelisted case
    with django_assert_num_queries(select=2):
        check_response(client.get(unrestricted_url), content_includes=case_text, content_type=content_type)

    ### can load blacklisted case while logged out, via redirect

    # first we get redirect to JS page
    check_response(client.get(url, follow=True), content_includes="Click here to continue")

    # POSTing will set our cookies and let the case load
    response = client.post(reverse('set_cookie'), {'not_a_bot': 'yes', 'next': url}, follow=True)
    check_response(response, content_includes=case_text, content_type=content_type)
    session = client.session
    assert session['case_allowance_remaining'] == settings.API_CASE_DAILY_ALLOWANCE - 1

    # we can now load directly
    response = client.get(url)
    check_response(response, content_includes=case_text, content_type=content_type)
    session = client.session
    assert session['case_allowance_remaining'] == settings.API_CASE_DAILY_ALLOWANCE - 2

    # can no longer load if quota used up
    session['case_allowance_remaining'] = 0
    session.save()
    response = client.get(url)
    if response_type == 'pdf':
        assert response.status_code == 302  # PDFs redirect back to HTML version if quota exhausted
    else:
        check_response(response)
        assert case_text not in response.content.decode()
    session = client.session
    assert session['case_allowance_remaining'] == 0

    # check daily quota reset
    session['case_allowance_last_updated'] -= 60 * 60 * 24 + 1
    session.save()
    response = client.get(url)
    check_response(response, content_includes=case_text, content_type=content_type)
    session = client.session
    assert session['case_allowance_remaining'] == settings.API_CASE_DAILY_ALLOWANCE - 1

    ### can load normally as logged-in user

    for c in [auth_client, token_auth_client]:
        response = c.get(url)
        check_response(response, content_includes=case_text, content_type=content_type)
        previous_case_allowance = c.auth_user.case_allowance_remaining
        c.auth_user.refresh_from_db()
        assert c.auth_user.case_allowance_remaining == previous_case_allowance - 1


@pytest.mark.django_db(databases=['default', 'capdb', 'user_data'])
def test_single_case_fastcase(client, fastcase_case_factory, elasticsearch):
    case_text = "Case HTML"
    case = fastcase_case_factory(body_cache__html=case_text, first_page_order=2, last_page_order=2)
    check_response(client.get(case.get_full_frontend_url()), content_includes=[case_text, "Case text courtesy of Fastcase"])


@pytest.mark.django_db(databases=['capdb'])
def test_case_series_name_redirect(client, unrestricted_case, elasticsearch):
    """ Test /series/volume/case/ with series redirect when not slugified"""
    cite = unrestricted_case.citations.first()
    cite_parts = re.match(r'(\S+)\s+(.*?)\s+(\S+)$', cite.cite).groups()

    # series is not slugified, expect redirect
    response = client.get(
        reverse('citation', args=[cite_parts[1], cite_parts[0], cite_parts[2]], host='cite'))
    check_response(response, status_code=302)

    response = client.get(
        reverse('citation', args=[cite_parts[1], cite_parts[0], cite_parts[2]], host='cite'), follow=True)
    check_response(response)

    # series redirect works with case_id
    response = client.get(
        reverse('citation', args=[cite_parts[1], cite_parts[0], cite_parts[2], unrestricted_case.id], host='cite'))
    check_response(response, status_code=302)

    response = client.get(
        reverse('citation', args=[cite_parts[1], cite_parts[0], cite_parts[2]], host='cite'), follow=True)
    check_response(response)


def get_schema(response):
    soup = BeautifulSoup(response.content.decode(), 'html.parser')
    scripts = soup.find_all('script', {'type': 'application/ld+json'})
    assert len(scripts) == 1
    script = scripts[0]
    return json.loads(script.string)

@pytest.mark.django_db(databases=['default', 'capdb'])
def test_schema_in_case(client, restricted_case, unrestricted_case, fastcase_case, elasticsearch):

    ### unrestricted case
    for case in (unrestricted_case, fastcase_case):
        response = client.get(case.get_full_frontend_url())
        check_response(response, content_includes=case.body_cache.html)

        schema = get_schema(response)
        assert schema["headline"] == case.name_abbreviation
        assert schema["author"]["name"] == case.court.name

        # if case is whitelisted, extra info about inaccessibility is not needed
        # https://developers.google.com/search/docs/data-types/paywalled-content
        assert "hasPart" not in schema

    ### blacklisted case

    response = client.post(reverse('set_cookie'), {'not_a_bot': 'yes', 'next': restricted_case.get_full_frontend_url()}, follow=True)
    check_response(response, content_includes=restricted_case.body_cache.html)
    schema = get_schema(response)
    assert schema["headline"] == restricted_case.name_abbreviation
    assert schema["author"]["name"] == restricted_case.court.name

    # if case is blacklisted, we include more data
    assert "hasPart" in schema
    assert schema["hasPart"]["isAccessibleForFree"] == 'False'


@pytest.mark.django_db(databases=['default', 'capdb'])
def test_schema_in_case_as_google_bot(client, restricted_case, elasticsearch):

    # our bot has seen too many cases!
    session = client.session
    session['case_allowance_remaining'] = 0
    session.save()
    assert session['case_allowance_remaining'] == 0

    with mock.patch('cite.views.is_google_bot', lambda request: True):
        response = client.get(restricted_case.get_full_frontend_url(), follow=True)
    assert not is_cached(response)

    # show cases anyway
    check_response(response, content_includes=restricted_case.body_cache.html)
    schema = get_schema(response)
    assert schema["headline"] == restricted_case.name_abbreviation
    assert schema["author"]["name"] == restricted_case.court.name
    assert "hasPart" in schema
    assert schema["hasPart"]["isAccessibleForFree"] == 'False'


@pytest.mark.django_db(databases=['default', 'capdb', 'user_data'])
def test_no_index(auth_client, case_factory, elasticsearch):
    case = case_factory(no_index=True)
    check_response(auth_client.get(case.get_full_frontend_url()), content_includes='content="noindex"')


@pytest.mark.django_db(databases=['capdb'])
def test_robots(client, case):
    case_string = "Disallow: %s" % case.frontend_url

    # default version is empty:
    url = reverse('robots', host='cite')
    response = client.get(url)
    check_response(response, content_type="text/plain", content_includes='User-agent: *', content_excludes=case_string)

    # case with robots_txt_until in future is included:
    case.no_index = True
    case.robots_txt_until = timezone.now() + timedelta(days=1)
    case.save()
    check_response(client.get(url), content_type="text/plain", content_includes=case_string)

    # case with robots_txt_until in past is excluded:
    case.robots_txt_until = timezone.now() - timedelta(days=1)
    case.save()
    response = client.get(url)
    check_response(response, content_type="text/plain", content_includes='User-agent: *', content_excludes=case_string)


@pytest.mark.django_db(databases=['capdb'])
def test_geolocation_log(client, unrestricted_case, elasticsearch, settings, caplog):
    """ Test state-level geolocation logging in case browser """
    if not Path(settings.GEOIP_PATH).exists():
        # only test geolocation if database file is available
        return
    settings.GEOLOCATION_FEATURE = True
    check_response(client.get(unrestricted_case.get_full_frontend_url(), HTTP_X_FORWARDED_FOR='128.103.1.1'))
    assert "Someone from Massachusetts, United States read a case" in caplog.text


### Extract single page image from a volume PDF with VolumeMetadata's extract_page_image ###

@pytest.mark.django_db(databases=['default', 'capdb'])
def test_retrieve_page_image(admin_client, auth_client, volume_metadata):
    volume_metadata.pdf_file = "fake_volume.pdf"
    volume_metadata.save()
    response = admin_client.get(reverse('page_image', args=[volume_metadata.pk, '2'], host='cite'))
    check_response(response, content_type="image/png")
    assert b'\x89PNG' in response.content

    response = auth_client.get(reverse('page_image', args=[volume_metadata.pk, '2'], host='cite'))
    check_response(response, status_code=302)


@pytest.mark.django_db(databases=["default", "capdb"])
def test_case_editor(
    reset_sequences, admin_client, auth_client, unrestricted_case_factory
):
    unrestricted_case = unrestricted_case_factory(first_page_order=1, last_page_order=3)
    url = reverse("case_editor", args=[unrestricted_case.pk], host="cite")
    response = admin_client.get(url)
    check_response(response)
    response = auth_client.get(url)
    check_response(response, status_code=302)

    # make an edit
    unrestricted_case.sync_case_body_cache()
    body_cache = unrestricted_case.body_cache
    old_html = body_cache.html
    old_first_page = unrestricted_case.first_page
    description = "Made some edits"
    page = unrestricted_case.structure.pages.first()
    response = admin_client.post(
        url,
        json.dumps(
            {
                "metadata": {
                    "name": [unrestricted_case.name, "new name"],
                    "decision_date_original": [
                        unrestricted_case.decision_date_original,
                        "2020-01-01",
                    ],
                    "first_page": [old_first_page, "ignore this"],
                    "human_corrected": [False, True],
                },
                "description": description,
                "edit_list": {
                    page.id: {
                        "BL_81.3": {
                            3: ["Case text 0", "Replacement text"],
                        }
                    }
                },
            }
        ),
        content_type="application/json",
    )
    check_response(response)

    # check OCR edit
    body_cache.refresh_from_db()
    new_html = body_cache.html
    assert list(unified_diff(old_html.splitlines(), new_html.splitlines(), n=0))[
        3:
    ] == [
        '-    <h4 class="parties" id="b81-4" data-blocks=\'[["BL_81.3",0,[226,1320,752,926]]]\'>Case text 0</h4>',
        '+    <h4 class="parties" id="b81-4" data-blocks=\'[["BL_81.3",0,[226,1320,752,926]]]\'>Replacement text</h4>',
    ]

    # check metadata
    unrestricted_case.refresh_from_db()
    assert unrestricted_case.name == "new name"
    assert unrestricted_case.decision_date_original == "2020-01-01"
    assert unrestricted_case.decision_date == datetime.date(year=2020, month=1, day=1)
    assert unrestricted_case.human_corrected is True
    assert unrestricted_case.first_page == old_first_page  # change ignored

    # check log
    log_entry = unrestricted_case.correction_logs.first()
    assert log_entry.description == description
    assert log_entry.user_id == admin_client.auth_user.id


@pytest.mark.django_db(databases=['capdb'])
def test_citations_page(client, case_factory, elasticsearch):
    dest_case = case_factory()
    dest_cite = dest_case.citations.first()
    source_cases = [case_factory() for _ in range(2)]
    for case in source_cases:
        set_case_text(case, dest_cite.cite)
        case.sync_case_body_cache()
    non_citing_case = case_factory()
    update_elasticsearch_from_queue()

    response = client.get(reverse('citations', host='cite')+f'?q={dest_case.pk}')
    check_response(
        response,
        content_includes=[c.name_abbreviation for c in source_cases],
        content_excludes=[non_citing_case.name_abbreviation]
    )


@pytest.mark.django_db(databases=['capdb'])
def test_random_case(client, case_factory, elasticsearch):
    """ Test random endpoint returns both cases eventually. """
    # set up two cases
    cases = set()
    found = set()
    for i in range(2):
        case = case_factory()
        CaseAnalysis(case=case, key='word_count', value=2000).save()
        cases.add(case.get_full_frontend_url())
    update_elasticsearch_from_queue()

    # try 20 times to get both
    for i in range(20):
        response = client.get(reverse('random', host='cite'))
        check_response(response, status_code=302)
        assert response.url in cases
        found.add(response.url)
        if found == cases:
            break
    else:
        raise Exception(f'Failed to redirect to {cases-found} after 20 tries.')


@pytest.mark.django_db(databases=['capdb', 'default', 'user_data'])
def test_redact_case_tool(admin_client, case, elasticsearch):
    case.sync_case_body_cache()
    update_elasticsearch_from_queue()
    response = admin_client.post(reverse('redact_case', args=[case.pk]), {'kind': 'redact', 'text': 'Case'})
    check_response(response)
    response = admin_client.post(reverse('redact_case', args=[case.pk]), {'kind': 'elide', 'text': 'text'})
    check_response(response)
    case.refresh_from_db()
    assert case.no_index_redacted == {"Case": "redacted"}
    assert case.no_index_elided == {"text": "..."}
    response = admin_client.get(case.get_full_frontend_url())
    check_response(response, content_includes=[
        "[ redacted ]",
        "<span class='elided-text' role='button' tabindex='0' data-hidden-text='text'>...</span>"
   ])
