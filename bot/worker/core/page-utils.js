const { log, error } = require('../utils/logger');
const { sleep } = require('../utils/helpers');
const TIMEOUT = 60000;

async function queryXPath(page, xpath) {
	if (typeof page.$x === 'function') {
		const elements = await page.$x(xpath);
		if (!elements.length) return null;
		const [first, ...rest] = elements;
		for (const el of rest) {
			try { await el.dispose(); } catch (_) {}
		}
		return first;
	}

	const handle = await page.evaluateHandle((xp) => {
		const result = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
		return result.singleNodeValue;
	}, xpath);
	const el = handle.asElement();
	if (!el) {
		await handle.dispose();
		return null;
	}
	return el;
}

async function waitForXPathCompat(page, xpath, opts = {}) {
	if (typeof page.waitForXPath === 'function') {
		return page.waitForXPath(xpath, opts);
	}

	const timeout = typeof opts.timeout === 'number' ? opts.timeout : TIMEOUT;
	const start = Date.now();

	while (Date.now() - start < timeout) {
		const el = await queryXPath(page, xpath);
		if (el) return el;
		await sleep(250);
	}

	throw new Error(`TimeoutError: Waiting for XPath ${xpath} failed: ${timeout}ms exceeded`);
}

// Базовые функции ожидания и взаимодействия
async function waitAndType(page, selector, text, callback = null) {
	await sleep(500);
	let element = null;
	try {
		element = await page.waitForSelector(selector, { timeout: TIMEOUT });
		await sleep(500);
		await element.click({ clickCount: 3 });
		await page.type(selector, text, { delay: 0 });
		await sleep(500);
		
		if (callback) {
			return await callback(page, element);
		}
		return true;
	} catch (err) {
		error(err);
		return false;
	} finally {
        if (element) {
            await element.dispose();
        }
	}
}

async function waitAndTypeX(page, xpath, text, callback = null) {
	await sleep(500);
    let element = null;
	try {
		element = await waitForXPathCompat(page, xpath, { timeout: TIMEOUT });
		await sleep(500);
		await element.click({ clickCount: 3 });
		await element.type(text, { delay: 0 });
		await sleep(500);
		
		if (callback) {
			return await callback(page, element);
		}
		
		return true;
	} catch (err) {
		error(err);
		return false;
	} finally {
        if (element) {
            await element.dispose();
        }
    }
}

async function waitAndClick(page, selector, callback = null) {
	await sleep(500);
    let element = null;
	try {
		element = await page.waitForSelector(selector, { timeout: TIMEOUT });
		await sleep(500);
		await element.click({ delay: 50 });
		await sleep(500);
		
		if (callback) {
			return await callback(page, element);
		}
		return true;
	} catch (err) {
		error(err);
		return false;
	} finally {
        if (element) {
            await element.dispose();
        }
    }
}

async function waitAndClickX(page, xpath, callback = null) {
	await sleep(500);
    let element = null;
	try {
		element = await waitForXPathCompat(page, xpath, { timeout: TIMEOUT });
		await sleep(500);
		await element.click();
		await sleep(500);
		
		if (callback) {
			return await callback(page, element);
		}

		return true;
	} catch (err) {
		error(err);
		return false;
	} finally {
        if (element) {
            await element.dispose();
        }
    }
}

// Проверка существования элемента
async function elementExists(page, selector) {
	try {
		if (selector.startsWith('//')) {
			const element = await queryXPath(page, selector);
			if (!element) return false;
			await element.dispose();
			return true;
		} else {
			const element = await page.$(selector);
			return element !== null;
		}
	} catch (err) {
		return false;
	}
}

async function clickIfExists(page, selector) {
    let element = null;
	try {
		if (selector.startsWith('//')) {
			element = await queryXPath(page, selector);
			if (!element) return false;
			await element.click();
			log(`Кликнут элемент: ${selector}`);
			await element.dispose();
			return true;
		} else {
			element = await page.$(selector);
			if (element) {
				await element.click();
				log(`Кликнут элемент: ${selector}`);
                await element.dispose();
				return true;
			}
		}
		return false;
	} catch (err) {
		return false;
	} finally {
        // Дополнительная очистка на случай ошибок
        if (element) {
            await element.dispose();
        }
    }
}

async function getCurrentUrl(page) {
	if (!page) throw new Error('getCurrentUrl: ctx.page is required');
	return page.url();
}

async function isCurrentUrlContains(page, needle, opts = {}) {
	if (!page) throw new Error('isCurrentUrlContains: ctx.page is required');
	if (typeof needle !== 'string' || needle.length === 0) {
		throw new Error('isCurrentUrlContains: needle must be a non-empty string');
	}

	const url = page.url();
	if (opts.caseInsensitive) {
		return url.toLowerCase().includes(needle.toLowerCase());
	}
	return url.includes(needle);
}

module.exports = {
	waitAndType,
	waitAndTypeX,
	waitAndClick,
	waitAndClickX,
	elementExists,
	clickIfExists,
	getCurrentUrl,
	isCurrentUrlContains
};