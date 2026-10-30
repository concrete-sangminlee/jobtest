/**
 * Read-only bridge for public HiBrain listings. No Slack/GitHub credentials.
 * Deploy as Web app, execute as yourself, access Anyone.
 * Full HTML is returned only by doGet(), never logged by testConnection().
 */
const HIBRAIN_LIST_URL = 'https://hibrain.net/recruitment/recruits?listType=D3NEW&pagesize=50&sortType=SORTDTM';

function collectListing_() {
  const urls = [HIBRAIN_LIST_URL, HIBRAIN_LIST_URL.replace('://hibrain.net', '://www.hibrain.net')];
  const results = [];
  for (const url of urls) {
    try {
      const response = UrlFetchApp.fetch(url, {
        method: 'get',
        headers: {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'ko-KR,ko;q=0.9'},
        followRedirects: true,
        muteHttpExceptions: true,
        validateHttpsCertificates: true
      });
      const code = response.getResponseCode();
      const html = response.getContentText('UTF-8');
      if (code === 200 && /id\s*=\s*["']articleList["']/.test(html)
          && /\/recruitment\/recruits\/\d+/.test(html)) {
        return {ok: true, version: 1, source: HIBRAIN_LIST_URL,
                fetched_at: new Date().toISOString(), html: html};
      }
      results.push('HTTP ' + code + ': no valid listing');
    } catch (_) {
      results.push('Fetch exception');
    }
  }
  return {ok: false, version: 1, error: results.join('; ')};
}

function doGet() {
  // Fixed source: callers cannot pass arbitrary destinations or secret headers.
  return ContentService.createTextOutput(JSON.stringify(collectListing_()))
    .setMimeType(ContentService.MimeType.JSON);
}

function testConnection() {
  const result = collectListing_();
  if (!result.ok) {
    throw new Error('Google에서 하이브레인 수집 실패: ' + result.error);
  }
  const ids = new Set(Array.from(result.html.matchAll(/\/recruitment\/recruits\/(\d+)/g), m => m[1]));
  console.log('연결 성공: 공고 링크 ' + ids.size + '개, ' + result.fetched_at);
  return {ok: true, link_count: ids.size, fetched_at: result.fetched_at};
}
