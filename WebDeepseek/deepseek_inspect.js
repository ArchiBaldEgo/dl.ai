const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());
const proxyChain = require('proxy-chain');
const data = require('/app/WebDeepseek/worker/data.json');

(async () => {
    const server = process.env.BOT_PROXY || process.env.PUPPETEER_PROXY_SERVER;
    const user = process.env.BOT_PROXY_USER || process.env.PUPPETEER_PROXY_USERNAME;
    const pass = process.env.BOT_PROXY_PASS || process.env.PUPPETEER_PROXY_PASSWORD;
    let proxyServer = server;
    if (server && user) {
        const host = server.replace(/^https?:\/\//, '');
        const upstream = 'http://' + encodeURIComponent(user) + ':' + encodeURIComponent(pass) + '@' + host;
        proxyServer = await proxyChain.anonymizeProxy(upstream);
    }

    const profileDir = '/app/WebDeepseek/worker/.chrome-profiles/bot-1';
    const browser = await puppeteer.launch({
        headless: 'new',
        protocolTimeout: 180000,
        userDataDir: profileDir,
        args: [
            (proxyServer ? '--proxy-server=' + proxyServer : ''),
            '--disable-gpu',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-extensions',
            '--disable-features=BackForwardCache',
        ].filter(Boolean)
    });

    const pages = await browser.pages();
    const page = pages[0] || await browser.newPage();
    await page.setViewport({ width: 800, height: 800 });

    await page.goto('https://chat.deepseek.com/', { waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise(r => setTimeout(r, 3000));

    // Enable DeepThink
    try {
        const xp = data.xpaths.chat.thinkingButtonDisabled.deepseek;
        const el = await page.waitForXPath(xp, { timeout: 10000, visible: true });
        if (el) { await el.click(); console.log('DeepThink enabled'); }
    } catch(e) { console.log('DeepThink toggle:', e.message); }

    await new Promise(r => setTimeout(r, 1000));

    // Type message
    try {
        const textarea = await page.waitForXPath('//textarea', { timeout: 10000, visible: true });
        await textarea.type('What is 2+2? Reply in one word.');
        await new Promise(r => setTimeout(r, 500));
        await page.keyboard.press('Enter');
        console.log('Message sent');
    } catch(e) { console.log('Send error:', e.message); }

    console.log('Waiting for generation...');
    for (let i = 0; i < 90; i++) {
        await new Promise(r => setTimeout(r, 2000));
        const stopFound = await page.evaluate(() => {
            const xpaths = [
                "//div[contains(@class,'stop') and not(contains(@class,'hidden'))]",
                "//div[@aria-label='stop' or @aria-label='Stop']",
                "//button[contains(@class,'stop')]",
            ];
            for (const xp of xpaths) {
                const res = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                const node = res.singleNodeValue;
                if (node) {
                    const rect = node.getBoundingClientRect();
                    if (rect && rect.width > 0 && rect.height > 0) return true;
                }
            }
            return false;
        });
        if (!stopFound && i > 3) { console.log('Generation done after ~' + (i*2) + 's'); break; }
    }

    // Wait a bit more for content to settle
    await new Promise(r => setTimeout(r, 3000));

    // Dump the HTML of the page chat area
    const html = await page.evaluate(() => {
        const messages = document.evaluate(
            "//div[contains(@class,'ds-message')]|//div[contains(@class,'message')]",
            document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const result = [];
        for (let i = 0; i < messages.snapshotLength; i++) {
            const node = messages.snapshotItem(i);
            result.push({
                index: i,
                className: node.className,
                tag: node.tagName,
                textPreview: node.textContent.substring(0, 300),
                childCount: node.children.length,
                childrenClasses: Array.from(node.children).map(c => c.className).slice(0, 10),
            });
        }
        // Also check for any new selectors
        const allDivs = document.querySelectorAll('div[class]');
        const classSet = new Set();
        for (const d of allDivs) {
            if (d.className.includes('message') || d.className.includes('markdown') || d.className.includes('think') || d.className.includes('reason')) {
                classSet.add(d.className);
            }
        }
        const dsMarkdownCount = document.querySelectorAll('div.ds-markdown, [class*=ds-markdown]').length;
        const markdownCount = document.querySelectorAll('div.markdown, [class*=markdown]').length;
        return {
            messageCount: messages.snapshotLength,
            messages: result.slice(-5),
            relevantClasses: Array.from(classSet).slice(0, 50),
            dsMarkdownCount,
            markdownCount,
        };
    });
    console.log(JSON.stringify(html, null, 2));

    await browser.close();
})().catch(e => { console.error(e); process.exit(1); });