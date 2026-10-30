"""``fm users`` — platform user rows, over routes that already exist.

Step 1 of retiring ``az containerapp exec``: the operator read ``public.user`` and
``project_user_rel`` through the exec channel at roughly one call per two minutes.
Both are already served by ``/admin/users*`` (platform-admin gated), so this is a
client, not an endpoint.

WHAT THESE TESTS ARE ACTUALLY GUARDING
---------------------------------------
Not the HTTP call — ``api.admin_get`` is tested elsewhere. The defect class this
command can introduce is **a report that is true about what it fetched and false
about what exists**: ``/admin/users`` is paginated (``{items,total,limit,offset}``)
and capped by ``MAX_LIMIT``, so a listing that prints 50 rows without saying there
are 312 has told the operator the platform has 50 users.

That is the same shape as the bulk endpoint's ``changed`` field and as
``applied_ids`` before it: a wrong account of a correct fetch, which never surfaces
on its own because the numbers it prints are all real.

⚠️ AND THE OPPOSITE FAILURE IS TESTED TOO. A truncation notice printed when nothing
was truncated is an alarm that cries wolf, and an operator who learns to ignore it
has lost the notice for the case that matters.
"""

from __future__ import annotations

import pytest

from formist_cli import users
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")


def _payload(n_items: int, total: int, limit: int = 50, offset: int = 0) -> dict:
    return {
        "items": [
            {
                "id": f"00000000-0000-0000-0000-{i:012d}",
                "email": f"user{i}@example.com",
                "name": f"User {i}",
                "is_platform_admin": i == 0,
                "waiting": False,
                "onboarding_completed": True,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
            for i in range(n_items)
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class TestAListingNeverOverstatesWhatItSaw:
    #: ⚠️ ASSERT ON THE NOTICE, NOT ON THE NUMBERS. The first version of these
    #: tests checked `"50" in out and "312" in out`, which the FALLBACK branch
    #: ("50 of 312") satisfies just as well as the truncation notice. A mutation
    #: that made page 2 claim to be the whole set passed. Three times tonight an
    #: assertion of mine has been satisfied by a mechanism other than the one
    #: under test; the fix is always to name the mechanism.
    TRUNCATED = "MORE EXIST"

    def test_a_truncated_listing_says_so_with_BOTH_numbers(self):
        out = users.render_list(_payload(50, 312))
        assert self.TRUNCATED in out, out
        assert "50" in out and "312" in out

    def test_it_names_the_flag_that_gets_the_rest(self):
        """Being told a view is partial is only useful with the way to widen it."""
        out = users.render_list(_payload(50, 312))
        assert "--offset" in out or "--limit" in out

    def test_a_COMPLETE_listing_does_not_cry_wolf(self):
        """An alarm that fires when nothing is wrong is an alarm nobody reads."""
        out = users.render_list(_payload(7, 7))
        assert self.TRUNCATED not in out
        assert "7 of 7" in out

    def test_an_empty_platform_is_not_reported_as_truncated(self):
        out = users.render_list(_payload(0, 0))
        assert self.TRUNCATED not in out
        assert "0 of 0" in out

    def test_a_MISSING_total_is_said_out_loud_not_papered_over(self):
        """The route always sends `total`. If it stops, a count with no denominator
        is the most confident-looking way to be wrong — so say it is unknown."""
        payload = _payload(5, 0)
        del payload["total"]
        out = users.render_list(payload)
        assert "unknown" in out.lower()
        assert self.TRUNCATED not in out

    def test_the_platform_admin_flag_is_visible_because_it_is_the_gate(self):
        """`is_platform_admin` decides who may use this whole surface.

        ⚠️ Asserted on the ROW, not on the word "admin" — the header contains it,
        so the first version of this test passed with every value dropped.
        """
        out = users.render_list(_payload(3, 3))
        admin_row = next(ln for ln in out.splitlines() if "user0@example.com" in ln)
        plain_row = next(ln for ln in out.splitlines() if "user1@example.com" in ln)
        assert "yes" in admin_row, admin_row
        assert "yes" not in plain_row, plain_row

    def test_offset_is_reflected_so_page_2_is_not_read_as_page_1(self):
        out = users.render_list(_payload(50, 312, offset=50))
        assert self.TRUNCATED in out
        assert "rows 51-100 of 312" in out, out

    def test_the_LAST_page_is_not_reported_as_truncated(self):
        """offset + shown == total. The boundary the off-by-one lives on."""
        out = users.render_list(_payload(12, 312, offset=300))
        assert self.TRUNCATED not in out, out

    def test_a_non_dict_payload_RAISES_rather_than_rendering_an_empty_table(self):
        """An empty table reads as "no users" — a wrong answer where an error is
        the right one. A server contract change must not look like a fact."""
        with pytest.raises(users.UsersError) as exc:
            users.render_list(users._as_dict(["not", "a", "dict"]))
        assert "contract" in str(exc.value).lower()


class TestSpacesForAUser:
    def test_it_renders_region_and_role(self):
        payload = {
            "items": [
                {"space_id": "452093ea-7b91-491e-88b6-3285edd48a31",
                 "name": "GFSS 2026", "region": "ae", "role": "OWNER"},
            ]
        }
        out = users.render_spaces(payload)
        assert "ae" in out and "OWNER" in out and "GFSS 2026" in out

    def test_a_user_in_no_spaces_says_so_rather_than_printing_nothing(self):
        """Blank output is indistinguishable from a command that failed quietly."""
        out = users.render_spaces({"items": []})
        assert out.strip()
        assert "no space" in out.lower() or "none" in out.lower()


class TestHelpSaysWhatTheSurfaceCannotDo:
    def test_it_says_there_is_no_server_side_search(self):
        """⚠️ `/admin/users` takes limit/offset and NOTHING else — no `q`. A
        `--help` that stays silent invites an operator to look for a flag that does
        not exist, or worse to assume an empty result means no such user."""
        text = users.build_standalone_parser().format_help().lower()
        assert "no server-side search" in text or "no search" in text

    def test_it_names_the_gate(self):
        text = users.build_standalone_parser().format_help()
        assert "platform admin" in text.lower()

    def test_it_says_which_database_these_rows_live_in(self):
        """Auth rows live in Global regardless of the space's region — an operator
        pointing this at `ae` and seeing nothing would otherwise conclude the user
        does not exist."""
        text = users.build_standalone_parser().format_help().lower()
        assert "global" in text


class TestTheSubjectIsValidated:
    def test_an_unknown_subcommand_is_refused_with_the_valid_ones(self):
        with pytest.raises(users.UsersError) as exc:
            users.canonical_subject("delete")
        assert "list" in str(exc.value)
        assert "spaces" in str(exc.value)

    def test_the_default_subject_is_the_listing(self):
        assert users.canonical_subject(None) == "list"
