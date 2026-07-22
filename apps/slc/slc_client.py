"""HTTP client for the UK Student Loans Company account overview."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin

import httpx2

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SERVICE_BASE = "https://www.manage-student-loan-balance.service.gov.uk"
ORS_URL = f"{SERVICE_BASE}/ors"
# Bare /summary without idp=2 redirects back to the landing chooser after login.
OVERVIEW_URL = f"{SERVICE_BASE}/ors/account-overview/secured/summary?idp=2&locale=en"
LOGIN_HOST = "https://logon.slc.co.uk"


class SlcError(Exception):
    """Raised when authentication or parsing fails."""


@dataclass(frozen=True)
class LoanSummary:
    """Parsed account overview fields from the SLC portal.

    Attributes:
        balance: Outstanding loan balance in GBP, if present.
        interest_rate_pct: Current interest rate as a percentage, if present.
        current_year: Academic year label for the summary section, if present.
        salary_repayments: Salary repayments for the year in GBP, if present.
        direct_repayments: Direct repayments for the year in GBP, if present.
        interest_added: Interest added for the year in GBP, if present.
    """

    balance: float | None
    interest_rate_pct: float | None
    current_year: str | None
    salary_repayments: float | None
    direct_repayments: float | None
    interest_added: float | None


def _page_title(html: str) -> str | None:
    """Extract the document title from an HTML page.

    Args:
        html: Raw HTML response body.

    Returns:
        The stripped title text, or None if no title element is found.
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() or None


def _error_messages(html: str) -> list[str]:
    """Collect user-facing error messages from an SLC HTML page.

    Args:
        html: Raw HTML response body.

    Returns:
        Deduplicated error strings found in GOV.UK error markup or known
        SLC credential/secret-answer copy.
    """
    messages: list[str] = []
    for match in re.finditer(
        r'class="[^"]*(?:govuk-error-message|govuk-error-summary__list)[^"]*"[^>]*>'
        r"(.*?)</(?:span|div|ul)>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = re.sub(r"\s+", " ", unescape(text)).strip()
        if text and text not in messages:
            messages.append(text)
    for match in re.finditer(
        r"(Your email address, CRN or password is not correct[^.<]*)",
        html,
        flags=re.IGNORECASE,
    ):
        text = match.group(1).strip()
        if text not in messages:
            messages.append(text)
    for match in re.finditer(
        r"(Your secret answer is not correct[^.<]*)",
        html,
        flags=re.IGNORECASE,
    ):
        text = match.group(1).strip()
        if text not in messages:
            messages.append(text)
    return messages


def _extract_inputs(html: str) -> dict[str, str]:
    """Extract named input values from HTML forms.

    Checkbox, submit, button, image, and file inputs are skipped.

    Args:
        html: Raw HTML response body.

    Returns:
        Mapping of input ``name`` attributes to their ``value`` attributes.
    """
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.IGNORECASE):
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        if not name_m:
            continue
        type_m = re.search(r'type=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        input_type = (type_m.group(1) if type_m else "text").lower()
        if input_type in {"checkbox", "submit", "button", "image", "file"}:
            continue
        value_m = re.search(r'value=["\']([^"\']*)["\']', tag, flags=re.IGNORECASE)
        fields[name_m.group(1)] = value_m.group(1) if value_m else ""
    return fields


def _first_form_action(html: str, base_url: str) -> str:
    """Resolve the first form action URL against a page URL.

    Args:
        html: Raw HTML response body.
        base_url: Absolute URL of the page containing the form.

    Returns:
        Absolute form action URL.

    Raises:
        SlcError: If no form element is present on the page.
    """
    form = re.search(r"<form\b([^>]*)>", html, flags=re.IGNORECASE)
    if not form:
        raise SlcError(f"No <form> found on page ({base_url})")
    action_m = re.search(
        r'action=["\']([^"\']*)["\']',
        form.group(1),
        flags=re.IGNORECASE,
    )
    action = action_m.group(1) if action_m else ""
    return urljoin(base_url, action)


def _element_text_by_id(html: str, element_id: str) -> str | None:
    """Return collapsed text content for an HTML element by id.

    Args:
        html: Raw HTML response body.
        element_id: Value of the element's ``id`` attribute.

    Returns:
        Whitespace-collapsed text content, or None if the element is missing.
    """
    pattern = (
        rf'<(?P<tag>\w+)\b[^>]*\bid=["\']{re.escape(element_id)}["\'][^>]*>'
        rf"(?P<body>.*?)</(?P=tag)>"
    )
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group("body"))
    return re.sub(r"\s+", " ", unescape(text)).strip() or None


def _parse_money(text: str | None) -> float | None:
    """Parse a GBP money amount from free text.

    Args:
        text: Text that may contain a currency amount.

    Returns:
        Parsed float value, or None if no amount can be parsed.
    """
    if not text:
        return None
    match = re.search(r"-?\s*£?\s*([\d,]+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_percent(text: str | None) -> float | None:
    """Parse a percentage value from free text.

    Args:
        text: Text that may contain a percentage.

    Returns:
        Parsed percentage as a float (for example ``4.5`` for ``4.5%``), or
        None if no percentage can be parsed.
    """
    if not text:
        return None
    match = re.search(r"([\d.]+)\s*%", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_overview(html: str) -> LoanSummary:
    """Parse account overview HTML into structured fields.

    Args:
        html: Raw HTML from the secured account overview page.

    Returns:
        Parsed loan summary values.

    Raises:
        SlcError: If the outstanding balance element cannot be parsed.
    """
    balance = _parse_money(_element_text_by_id(html, "balanceId_1"))
    interest_rate = _parse_percent(_element_text_by_id(html, "interestAsOfDateId-1"))
    year_text = _element_text_by_id(html, "academicYearSummaryId-1")
    current_year = None
    if year_text:
        current_year = (
            re.sub(r"\s*summary\s*$", "", year_text, flags=re.IGNORECASE).strip() or None
        )

    summary = LoanSummary(
        balance=balance,
        interest_rate_pct=interest_rate,
        current_year=current_year,
        salary_repayments=_parse_money(
            _element_text_by_id(html, "salaryRepaymentAmountId-1"),
        ),
        direct_repayments=_parse_money(
            _element_text_by_id(html, "directRepaymentAmountId-1"),
        ),
        interest_added=_parse_money(_element_text_by_id(html, "interestAddedAmountId-1")),
    )
    if summary.balance is None:
        title = _page_title(html) or "unknown page"
        raise SlcError(
            f"Could not parse balance from overview (title={title!r}). "
            "Login may have failed or the portal HTML changed.",
        )
    return summary


def _client() -> httpx2.Client:
    """Create an HTTP client configured for the SLC portal.

    Returns:
        An ``httpx2.Client`` with browser-like headers, redirect following, and
        a 45 second timeout.
    """
    return httpx2.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        },
        follow_redirects=True,
        timeout=45.0,
    )


def _reject_cookies(client: httpx2.Client) -> None:
    """Reject non-essential cookies on the SLC service landing page.

    Args:
        client: Authenticated session client used for subsequent requests.
    """
    client.post(
        f"{ORS_URL}?cookies-consent",
        data={
            "currentPage": f"{ORS_URL}?locale=null",
            "locale": "null",
            "idp": "",
            "currentPageLang": "en",
            "cookiesValue": "reject-all",
        },
    )


def _open_login_page(client: httpx2.Client) -> httpx2.Response:
    """Navigate from the ORS landing page to the SLC credential form.

    Args:
        client: Session client used for cookie and redirect handling.

    Returns:
        Response for the SLC login form page.

    Raises:
        SlcError: If the login form fields are not present in the response.
    """
    client.get(ORS_URL)
    _reject_cookies(client)
    login = client.post(
        ORS_URL,
        data={
            "locale": "null",
            "idp": "",
            "loginType": "SIGN_IN",
            "ors-chapter": "",
        },
    )
    if "userId" not in login.text or "_csrf" not in login.text:
        raise SlcError(
            f"Did not reach SLC login form "
            f"(url={login.url}, title={_page_title(login.text)!r}). "
            "Portal may be behind Queue-it or the flow changed.",
        )
    return login


def _post_credentials(
    client: httpx2.Client,
    login_response: httpx2.Response,
    *,
    username: str,
    password: str,
) -> httpx2.Response:
    """Submit username and password to the SLC login form.

    Args:
        client: Session client used for cookie and redirect handling.
        login_response: Response containing the credential form.
        username: Email address or customer reference number.
        password: Account password.

    Returns:
        Response for the secret-answer page.

    Raises:
        SlcError: If required hidden fields are missing, credentials are
            rejected, or the secret-answer page is not returned.
    """
    fields = _extract_inputs(login_response.text)
    for required in ("_csrf", "lt", "execution", "_eventId"):
        if required not in fields:
            raise SlcError(f"Login form missing hidden field: {required}")
    fields["userId"] = username
    fields["password"] = password
    fields["continue-button"] = ""

    post_url = _first_form_action(login_response.text, str(login_response.url))
    response = client.post(
        post_url,
        data=fields,
        headers={"Origin": LOGIN_HOST, "Referer": str(login_response.url)},
    )

    errors = _error_messages(response.text)
    if errors and "secretAnswer" not in response.text:
        raise SlcError(f"Credential login failed: {errors[0]}")
    if "secretAnswer" not in response.text:
        raise SlcError(
            "Expected secret-answer page after credentials "
            f"(url={response.url}, title={_page_title(response.text)!r})",
        )
    return response


def _post_secret_answer(
    client: httpx2.Client,
    secret_page: httpx2.Response,
    *,
    secret_answer: str,
) -> httpx2.Response:
    """Submit the secret answer and complete the Okta redirect chain.

    Args:
        client: Session client used for cookie and redirect handling.
        secret_page: Response containing the secret-answer form.
        secret_answer: Account secret answer.

    Returns:
        Final response after secret submission, typically the overview page.

    Raises:
        SlcError: If the CSRF token is missing or the secret answer is rejected.
    """
    fields = _extract_inputs(secret_page.text)
    if "_csrf" not in fields:
        csrf = re.search(
            r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']',
            secret_page.text,
            flags=re.IGNORECASE,
        ) or re.search(
            r'value=["\']([^"\']+)["\'][^>]*name=["\']_csrf["\']',
            secret_page.text,
            flags=re.IGNORECASE,
        )
        if csrf:
            fields["_csrf"] = csrf.group(1)
        else:
            raise SlcError("Secret-answer form missing _csrf token")
    fields["secretAnswer"] = secret_answer
    fields["continue-button"] = ""

    post_url = _first_form_action(secret_page.text, str(secret_page.url))
    response = client.post(
        post_url,
        data=fields,
        headers={"Origin": LOGIN_HOST, "Referer": str(secret_page.url)},
    )

    errors = _error_messages(response.text)
    if errors and "secretAnswer" in response.text:
        raise SlcError(f"Secret answer failed: {errors[0]}")
    return response


def _ensure_overview(
    client: httpx2.Client,
    after_secret: httpx2.Response,
) -> httpx2.Response:
    """Return a response that contains the balance overview markup.

    Successful secret-answer auth already redirects through Okta onto the
    overview page. A follow-up GET to ``/summary`` without ``idp=2`` drops the
    session back to the landing chooser.

    Args:
        client: Session client used for cookie and redirect handling.
        after_secret: Response returned after secret-answer submission.

    Returns:
        Response whose body includes the ``balanceId_1`` element.

    Raises:
        SlcError: If none of the overview candidate URLs contain the balance.
    """
    if "balanceId_1" in after_secret.text:
        return after_secret

    candidates = [
        str(after_secret.url),
        OVERVIEW_URL,
        f"{SERVICE_BASE}/ors/account-overview/secured/summary?idp=2",
    ]
    last: httpx2.Response | None = None
    for url in candidates:
        response = client.get(url)
        last = response
        if "balanceId_1" in response.text:
            return response

    if last is None:
        raise SlcError("Overview page missing balance element (no responses)")
    raise SlcError(
        "Overview page missing balance element "
        f"(url={last.url}, title={_page_title(last.text)!r}, "
        f"status={last.status_code})",
    )


def fetch_loan_summary(
    *,
    username: str,
    password: str,
    secret_answer: str,
) -> LoanSummary:
    """Authenticate to the SLC portal and return the parsed loan summary.

    Args:
        username: Email address or customer reference number.
        password: Account password.
        secret_answer: Account secret answer.

    Returns:
        Parsed overview values including balance and repayment fields.

    Raises:
        SlcError: If login, secret answer, or overview parsing fails.
        httpx2.HTTPError: If an HTTP transport error occurs.
    """
    with _client() as client:
        login_page = _open_login_page(client)
        secret_page = _post_credentials(
            client,
            login_page,
            username=username,
            password=password,
        )
        after_secret = _post_secret_answer(
            client,
            secret_page,
            secret_answer=secret_answer,
        )
        overview = _ensure_overview(client, after_secret)
        return parse_overview(overview.text)
