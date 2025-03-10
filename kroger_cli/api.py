import asyncio
import json
import re
import datetime
import kroger_cli.cli
from kroger_cli.memoize import memoized
from kroger_cli import helper
from pyppeteer import launch


class KrogerAPI:
    browser_options = {
        'headless': False,
        'devtools': True,
        'args': ['--blink-settings=imagesEnabled=false'],
        'executablePath': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    }
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/81.0.4044.129 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    def __init__(self, cli):
        self.cli: kroger_cli.cli.KrogerCLI = cli

    def complete_survey(self):
        # Cannot use headless mode here for some reason (sign-in cookie doesn't stick)
        self.browser_options['headless'] = False
        res = asyncio.get_event_loop().run_until_complete(self._complete_survey())
        self.browser_options['headless'] = True

        return res

    @memoized
    def get_account_info(self):
        return asyncio.get_event_loop().run_until_complete(self._get_account_info())

    @memoized
    def get_points_balance(self):
        return asyncio.get_event_loop().run_until_complete(self._get_points_balance())

    def clip_coupons(self):
        return asyncio.get_event_loop().run_until_complete(self._clip_coupons())

    @memoized
    def get_purchases_summary(self):
        return asyncio.get_event_loop().run_until_complete(self._get_purchases_summary())

    async def _retrieve_feedback_url(self):
        self.cli.console.print('Loading `My Purchases` page (to retrieve the Feedback’s Entry ID)')

        # Model overlay pop up (might not exist)
        # Need to click on it, as it prevents me from clicking on `Order Details` link
        try:
            await self.page.waitForSelector('.ModalitySelectorDynamicTooltip--Overlay', {'timeout': 10000})
            await self.page.click('.ModalitySelectorDynamicTooltip--Overlay')
        except Exception:
            pass

        try:
            # `See Order Details` link
            await self.page.waitForSelector('.PurchaseCard-top-view-details-button', {'timeout': 10000})
            await self.page.click('.PurchaseCard-top-view-details-button a')
            # `View Receipt` link
            await self.page.waitForSelector('.PurchaseCard-top-view-details-button a', {'timeout': 10000})
            await self.page.click('.PurchaseCard-top-view-details-button a')
            content = await self.page.content()
        except Exception:
            link = 'https://www.' + self.cli.config['main']['domain'] + '/mypurchases'
            self.cli.console.print('[bold red]Couldn’t retrieve the latest purchase, please make sure it exists: '
                                   '[link=' + link + ']' + link + '[/link][/bold red]')
            raise Exception

        try:
            match = re.search('Entry ID: (.*?) ', content)
            entry_id = match[1]
            match = re.search('Date: (.*?) ', content)
            entry_date = match[1]
            match = re.search('Time: (.*?) ', content)
            entry_time = match[1]
            self.cli.console.print('Entry ID retrieved: ' + entry_id)
        except Exception:
            self.cli.console.print('[bold red]Couldn’t retrieve Entry ID from the receipt, please make sure it exists: '
                                   '[link=' + self.page.url + ']' + self.page.url + '[/link][/bold red]')
            raise Exception

        entry = entry_id.split('-')
        hour = entry_time[0:2]
        minute = entry_time[3:5]
        meridian = entry_time[5:7].upper()
        date = datetime.datetime.strptime(entry_date, '%m/%d/%y')
        full_date = date.strftime('%m/%d/%Y')
        month = date.strftime('%m')
        day = date.strftime('%d')
        year = date.strftime('%Y')

        url = f'https://www.krogerstoresfeedback.com/Index.aspx?' \
              f'CN1={entry[0]}&CN2={entry[1]}&CN3={entry[2]}&CN4={entry[3]}&CN5={entry[4]}&CN6={entry[5]}&' \
              f'Index_VisitDateDatePicker={month}%2f{day}%2f{year}&' \
              f'InputHour={hour}&InputMeridian={meridian}&InputMinute={minute}'

        return url, full_date

    async def _complete_survey(self):
        signed_in = await self.sign_in_routine(redirect_url='/mypurchases', contains=['My Purchases'])
        if not signed_in:
            await self.destroy()
            return None

        try:
            url, survey_date = await self._retrieve_feedback_url()
        except Exception:
            await self.destroy()
            return None

        await self.page.goto(url)
        await self.page.waitForSelector('#Index_VisitDateDatePicker', {'timeout': 10000})
        # We need to manually set the date, otherwise the validation fails
        js = "() => {$('#Index_VisitDateDatePicker').datepicker('setDate', '" + survey_date + "');}"
        await self.page.evaluate(js)
        await self.page.click('#NextButton')

        for i in range(35):
            current_url = self.page.url
            try:
                await self.page.waitForSelector('#NextButton', {'timeout': 5000})
            except Exception:
                if 'Finish' in current_url:
                    await self.destroy()
                    return True
            await self.page.evaluate(helper.get_survey_injection_js(self.cli.config))
            await self.page.click('#NextButton')

        await self.destroy()
        return False

    async def _get_account_info(self):
        signed_in = await self.sign_in_routine()
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading profile info..')
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/accountmanagement/api/profile')
        try:
            content = await self.page.content()
            profile = self._get_json_from_page_content(content)
            user_id = profile['userId']
        except Exception:
            profile = None
        await self.destroy()

        return profile

    async def _get_points_balance(self):
        signed_in = await self.sign_in_routine()
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading points balance..')
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/accountmanagement/api/points-summary')
        try:
            content = await self.page.content()
            balance = self._get_json_from_page_content(content)
            program_balance = balance[0]['programBalance']['balance']
        except Exception:
            balance = None
        await self.destroy()

        return balance

    async def _clip_coupons(self):
        signed_in = await self.sign_in_routine(redirect_url='/cl/coupons/', contains=['Coupons Clipped'])
        if not signed_in:
            await self.destroy()
            return None

        js = """
            window.scrollTo(0, document.body.scrollHeight);
            for (let i = 0; i < 150; i++) {
                let el = document.getElementsByClassName('kds-Button--favorable')[i];
                if (el !== undefined) {
                    el.scrollIntoView();
                    el.click();
                }
            }
        """

        self.cli.console.print('[italic]Applying the coupons, please wait..[/italic]')
        await self.page.keyboard.press('Escape')
        for i in range(6):
            await self.page.evaluate(js)
            await self.page.keyboard.press('End')
            await self.page.waitFor(1000)
        await self.page.waitFor(3000)
        await self.destroy()
        self.cli.console.print('[bold]Coupons successfully clipped to your account! :thumbs_up:[/bold]')

    async def _get_purchases_summary(self):
        signed_in = await self.sign_in_routine()
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading your purchases..')
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/mypurchases/api/v1/receipt/summary/by-user-id')
        try:
            content = await self.page.content()
            data = self._get_json_from_page_content(content)
        except Exception:
            data = None
        await self.destroy()

        return data

    async def onReq(self, request):
        regex = re.compile("clarity|pinterest|adobe|mbox|ruxitagentjs|akam|sstats.kroger.com|rb_[A-Za-z0-9]{8}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{12}")
        url = request.url
        if (regex.search(url)):
            await request.abort()
            print(url)

        else:
            await request.continue_()

    async def init(self):
        self.browser = await launch(self.browser_options)
        self.page = await self.browser.newPage()
        await self.page.setUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/67.0.3396.99 Safari/537.36")
        await self.page.setExtraHTTPHeaders(self.headers)
        await self.page.setViewport({'width': 700, 'height': 0})
        await self.page.evaluateOnNewDocument("""
        () => {
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false
            })
        }
        """)
        await self.page.setRequestInterception(True)
        self.page.on('request', self.onReq)

    async def destroy(self):
        await self.page.close()
        await self.browser.close()

    async def sign_in_routine(self, redirect_url='/account/update', contains=None):
        if contains is None and redirect_url == '/account/update':
            contains = ['Profile Information']

        await self.init()
        self.cli.console.print('[italic]Signing in.. (please wait, it might take awhile)[/italic]')
        signed_in = await self.sign_in(redirect_url, contains)

        if not signed_in and self.browser_options['headless']:
            self.cli.console.print('[red]Sign in failed. Trying one more time..[/red]')
            self.browser_options['headless'] = False
            await self.destroy()
            await self.init()
            signed_in = await self.sign_in(redirect_url, contains)

        if not signed_in:
            self.cli.console.print('[bold red]Sign in failed. Please make sure the username/password is correct.'
                                   '[/bold red]')

        return signed_in

    async def sign_in(self, redirect_url, contains):
        timeout = 20000
        if not self.browser_options['headless']:
            timeout = 60000
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/signin?redirectUrl=' + redirect_url)
        dimensions = await self.page.evaluate('''() => {
        return {
            width: document.documentElement.clientWidth,
            height: document.documentElement.clientHeight,
            deviceScaleFactor: window.devicePixelRatio,
        }
    }''')

        print(dimensions)
        await self.page.click('#SignIn-emailInput', {'clickCount': 3})  # Select all in the field
        await self.page.type('#SignIn-emailInput', self.cli.username)
        await self.page.click('#SignIn-passwordInput', {'clickCount': 3})
        await self.page.type('#SignIn-passwordInput', self.cli.password)
        await self.page.keyboard.press('Enter')
        try:
            await self.page.waitForNavigation(timeout=timeout)
        except Exception:
            return False

        if contains is not None:
            html = await self.page.content()
            for item in contains:
                if item not in html:
                    return False

        return True

    def _get_json_from_page_content(self, content):
        match = re.search('<pre.*?>(.*?)</pre>', content)
        return json.loads(match[1])
