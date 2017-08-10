from darecms.common import *


class HTTPRedirect(cherrypy.HTTPRedirect):
    """
    CherryPy uses exceptions to indicate things like HTTP 303 redirects.  This
    subclasses the standard CherryPy exception to add string formatting and
    automatic quoting.  So instead of saying
        raise HTTPRedirect('foo?message={}'.format(quote(bar)))
    we can say
        raise HTTPRedirect('foo?message={}', bar)

    EXTREMELY IMPORTANT: If you pass in a relative URL, this class will use the
    current querystring to build an absolute URL.  Therefore it's EXTREMELY IMPORTANT
    that the only time you create this class is in the context of a pageload.

    Do not save copies this class, only create it on-demand when needed as part of a 'raise' statement.
    """
    def __init__(self, page, *args, **kwargs):
        save_location = kwargs.pop('save_location', False)

        args = [self.quote(s) for s in args]
        kwargs = {k: self.quote(v) for k, v in kwargs.items()}
        query = page.format(*args, **kwargs)

        if save_location and cherrypy.request.method == 'GET':
            # remember the original URI the user was trying to reach.
            # useful if we want to redirect the user back to the same page after
            # they complete an action, such as logging in
            # example URI: '/uber/registration/form?id=786534'
            original_location = cherrypy.request.wsgi_environ['REQUEST_URI']

            # note: python does have utility functions for this. if this gets any more complex, use the urllib module
            qs_char = '?' if '?' not in query else '&'
            query += "{sep}original_location={loc}".format(sep=qs_char, loc=self.quote(original_location))

        cherrypy.HTTPRedirect.__init__(self, query)

    def quote(self, s):
        return quote(s) if isinstance(s, str) else str(s)


def create_valid_user_supplied_redirect_url(url, default_url):
    """
    Create a valid redirect from user-supplied data.

    If there is invalid data, or a security issue is detected, then ignore and redirect to the homepage

    :param url: user-supplied URL that is being requested to redirect to
    :param default_url: the name of the URL we should redirect to if there's an issue
    :return: a secure and valid URL that we allow a redirect to be made to
    """

    # security: ignore cross-site redirects that aren't for local pages.
    # i.e. if an attacker passes in 'original_location=https://badsite.com/stuff/" then just ignore it
    parsed_url = urlparse(url)
    security_issue = parsed_url.scheme or parsed_url.netloc

    if not url or 'login' in url or security_issue:
        return default_url

    return url


def localized_now():
    """Returns datetime.now() but localized to the event's configured timezone."""
    return localize_datetime(datetime.utcnow())


def localize_datetime(dt):
    return dt.replace(tzinfo=UTC).astimezone(c.EVENT_TIMEZONE)


def comma_and(xs):
    """
    Accepts a list of strings and separates them with commas as grammatically
    appropriate with an "and" before the final entry.  For example:
        ['foo']               => 'foo'
        ['foo', 'bar']        => 'foo and bar'
        ['foo', 'bar', 'baz'] => 'foo, bar, and baz'
    """
    if len(xs) > 1:
        xs[-1] = 'and ' + xs[-1]
    return (', ' if len(xs) > 2 else ' ').join(xs)


def check_csrf(csrf_token):
    """
    Accepts a csrf token (and checks the request headers if None is provided)
    and compares it to the token stored in the session.  An exception is raised
    if the values do not match or if no token is found.
    """
    if csrf_token is None:
        csrf_token = cherrypy.request.headers.get('CSRF-Token')
    assert csrf_token, 'CSRF token missing'
    if csrf_token != cherrypy.session['csrf_token']:
        log.error("csrf tokens don't match: {!r} != {!r}", csrf_token, cherrypy.session['csrf_token'])
        raise AssertionError('CSRF check failed')
    else:
        cherrypy.request.headers['CSRF-Token'] = csrf_token


def check(model, *, prereg=False):
    """
    Runs all default validations against the supplied model instance.  Returns
    either a string error message if any validation fails and returns None if
    all validations passed.
    """
    for field, name in model.required:
        if not str(getattr(model, field)).strip():
            return name + ' is a required field'

    for v in [sa.validation.validations] + ([sa.prereg_validation.validations] if prereg else []):
        for validator in v[model.__class__.__name__].values():
            message = validator(model)
            if message:
                return message


class Order:
    def __init__(self, order):
        self.order = order

    def __getitem__(self, field):
        return ('-' + field) if field == self.order else field

    def __str__(self):
        return self.order


class Registry:
    """
    Base class for configurable registries such as the Dept Head Checklist and
    event-specific features such as MAGFest Season Pass events.
    """
    @classmethod
    def register(cls, slug, kwargs):
        cls.instances[slug] = cls(slug, **kwargs)


def hour_day_format(dt):
    """
    Accepts a localized datetime object and returns a formatted string showing
    only the day and hour, e.g "7pm Thu" or "10am Sun".
    """
    return dt.astimezone(c.EVENT_TIMEZONE).strftime('%I%p ').strip('0').lower() + dt.astimezone(c.EVENT_TIMEZONE).strftime('%a')


def send_email(source, dest, subject, body, format='text', cc=(), bcc=(), model=None, ident=None):
    subject = subject.format(c=c)
    to, cc, bcc = map(listify, [dest, cc, bcc])
    ident = ident or subject
    if c.DEV_BOX:
        for xs in [to, cc, bcc]:
            xs[:] = [email for email in xs if email.endswith('mailinator.com') or c.DEVELOPER_EMAIL in email]

    if c.SEND_EMAILS and to:
         message = EmailMessage(subject=subject, **{'bodyText' if format == 'text' else 'bodyHtml': body})
         AmazonSES(c.AWS_ACCESS_KEY, c.AWS_SECRET_KEY).sendEmail(
             source=source,
             toAddresses=to,
             ccAddresses=cc,
             bccAddresses=bcc,
             message=message
         )
         sleep(0.1)  # avoid hitting rate limit
         pass
    else:
        log.error('email sending turned off, so unable to send {}', locals())

    if model and dest:
        body = body.decode('utf-8') if isinstance(body, bytes) else body
        fk = {'model': 'n/a'} if model == 'n/a' else {'fk_id': model.id, 'model': model.__class__.__name__}
        _record_email_sent(sa.Email(subject=subject, dest=','.join(listify(dest)), body=body, ident=ident, **fk))


def _record_email_sent(email):
    """
    Save in our database the contents of the Email model passed in.
    We'll use this for history tracking, and to know that we shouldn't re-send this email because it already exists

    note: This is in a separate function so we can unit test it
    """
    with sa.Session() as session:
        session.add(email)


def report_critical_exception(msg, subject="Critical Error"):
    """
    Report an exception to the loggers with as much context (request params/etc) as possible, and send an email.

    Call this function when you really want to make some noise about something going really badly wrong.

    :param msg: message to prepend to output
    :param subject: optional: subject for emails going out
    """

    # log with lots of cherrypy context in here
    darecms.server.log_exception_with_verbose_context(msg)

    # also attempt to email the admins
    # TODO: Don't hardcode emails here.
    send_email(c.ADMIN_EMAIL, [c.ADMIN_EMAIL, 'dare@magfest.org'], subject, msg + '\n{}'.format(traceback.format_exc()))


def get_page(page, queryset):
    return queryset[(int(page) - 1) * 100: int(page) * 100]


def genpasswd():
    """
    Admin accounts have passwords auto-generated; this function tries to combine
    three random dictionary words but returns a string of 8 random characters if
    no dictionary is installed.
    """
    try:
        with open('/usr/share/dict/words') as f:
            words = [s.strip() for s in f.readlines() if "'" not in s and s.islower() and 3 < len(s) < 8]
            return ' '.join(random.choice(words) for i in range(4))
    except:
        return ''.join(chr(randrange(33, 127)) for i in range(8))


def static_overrides(dirname):
    """
    We want plugins to be able to specify their own static files to override the
    ones which we provide by default.  The main files we expect to be overridden
    are the theme image files, but theoretically a plugin can override anything
    it wants by calling this method and passing its static directory.
    """
    appconf = cherrypy.tree.apps[c.PATH].config
    basedir = os.path.abspath(dirname).rstrip('/')
    for dpath, dirs, files in os.walk(basedir):
        relpath = dpath[len(basedir):]
        for fname in files:
            appconf['/static' + relpath + '/' + fname] = {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(dpath, fname)
            }


def mount_site_sections(module_root):
    from darecms.server import Root
    sections = [path.split('/')[-1][:-3] for path in glob(os.path.join(module_root, 'site_sections', '*.py'))
                                         if not path.endswith('__init__.py')]
    for section in sections:
        module = importlib.import_module(basename(module_root) + '.site_sections.' + section)
        setattr(Root, section, module.Root())


def convert_to_absolute_url(relative_uber_page_url):
    """
    In ubersystem, we always use relative url's of the form "../{some_site_section}/{somepage}"
    We use relative URLs so that no matter what proxy server we are behind on the web, it always works.

    We normally avoid using absolute URLs at al costs, but sometimes it's needed when creating URLs for
    use with emails or CSV exports.  In that case, we need to take a relative URL and turn it into
    an absolute URL.

    Do not use this function unless you absolutely need to, instead use relative URLs as much as possible.
    """

    if not relative_uber_page_url:
        return ''

    if relative_uber_page_url[:3] != '../':
        raise ValueError("relative url MUST start with '../'")

    return urljoin(c.URL_BASE + "/", relative_uber_page_url[3:])


_when_dateformat = "%m/%d"


class DateBase:
    @staticmethod
    def now():
        # This exists so we can patch this in unit tests
        return localized_now()


class days_before(DateBase):
    """
    Returns true if today is # days before a deadline.

    :param: days - number of days before deadline to start
    :param: deadline - datetime of the deadline
    :param: until - (optional) number of days prior to deadline to end (default: 0)

    Examples:
        days_before(45, c.POSITRON_BEAM_DEADLINE)() - True if it's 45 days before c.POSITRON_BEAM_DEADLINE
        days_before(10, c.WARP_COIL_DEADLINE, 2)() - True if it's between 10 and 2 days before c.WARP_COIL_DEADLINE
    """
    def __init__(self, days, deadline, until=None):
        if days <= 0:
            raise ValueError("'days' paramater must be > 0. days={}".format(days))

        if until and days <= until:
            raise ValueError("'days' paramater must be less than 'until'. days={}, until={}".format(days, until))

        self.days, self.deadline, self.until = days, deadline, until

        if deadline:
            self.starting_date = self.deadline - timedelta(days=self.days)
            self.ending_date = deadline if not until else (deadline - timedelta(days=until))

            assert self.starting_date < self.ending_date

    def __call__(self):
        if not self.deadline:
            return False

        return self.starting_date < self.now() < self.ending_date

    @property
    def active_when(self):
        if not self.deadline:
            return ''

        start_txt = self.starting_date.strftime(_when_dateformat)
        end_txt = self.ending_date.strftime(_when_dateformat)

        return 'between {} and {}'.format(start_txt, end_txt)


class before(DateBase):
    """
    Returns true if today is anytime before a deadline.

    :param: deadline - datetime of the deadline

    Examples:
        before(c.POSITRON_BEAM_DEADLINE)() - True if it's before c.POSITRON_BEAM_DEADLINE
    """
    def __init__(self, deadline):
        self.deadline = deadline

    def __call__(self):
        return bool(self.deadline) and self.now() < self.deadline

    @property
    def active_when(self):
        return 'before {}'.format(self.deadline.strftime(_when_dateformat)) if self.deadline else ''


class days_after(DateBase):
    """
    Returns true if today is at least a certain number of days after a deadline.

    :param: days - number of days after deadline to start
    :param: deadline - datetime of the deadline

    Examples:
        days_after(6, c.TRANSPORTER_ROOM_DEADLINE)() - True if it's at least 6 days after c.TRANSPORTER_ROOM_DEADLINE
    """
    def __init__(self, days, deadline):
        if days is None:
            days = 0

        if days < 0:
            raise ValueError("'days' paramater must be >= 0. days={}".format(days))

        self.starting_date = None if not deadline else deadline + timedelta(days=days)

    def __call__(self):
        return bool(self.starting_date) and (self.now() > self.starting_date)

    @property
    def active_when(self):
        return 'after {}'.format(self.starting_date.strftime(_when_dateformat)) if self.starting_date else ''


class request_cached_context:
    """
    We cache certain variables (like c.BADGES_SOLD) on a per-cherrypy.request basis.
    There are situation situations, like unit tests or non-HTTP request contexts (like daemons) where we want to
    carefully control this behavior, or where the cache will never be reset.

    When this class is finished it will clear the per-request cache.

    example of how to use:
    with request_cached_context():
        # do things that use the cached values, and after this block is done, the values won't be cached anymore.
    """

    def __init__(self, clear_cache_on_start=False):
        self.clear_cache_on_start = clear_cache_on_start

    def __enter__(self):
        if self.clear_cache_on_start:
            request_cached_context._clear_cache()

    def __exit__(self, type, value, traceback):
        request_cached_context._clear_cache()

    @staticmethod
    def _clear_cache():
        threadlocal.clear()
