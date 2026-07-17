import unittest
from bs4 import BeautifulSoup
from src.monitor import extract_anchor_jobs, extract_jsonld_jobs


class MonitorTests(unittest.TestCase):
    def test_extract_anchor_job(self):
        soup = BeautifulSoup(
            '<div><a href="/jobs/123">Working Student DevOps & Python (m/f/d)</a><span>Munich, Germany</span></div>',
            'html.parser',
        )
        jobs = extract_anchor_jobs(soup, 'Example', 'https://example.com/careers', 'example.com')
        self.assertEqual(len(jobs), 1)
        self.assertIn('DevOps', jobs[0].title)

    def test_extract_jsonld_job(self):
        soup = BeautifulSoup(
            '''<script type="application/ld+json">{
              "@type":"JobPosting", "title":"Internship Embedded Software",
              "description":"Python, Linux and test automation", "url":"https://example.com/jobs/1",
              "jobLocation":{"address":{"addressLocality":"Ingolstadt","addressCountry":"Germany"}}
            }</script>''',
            'html.parser',
        )
        jobs = extract_jsonld_jobs(soup, 'Example', 'https://example.com')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, 'Ingolstadt, Germany')


if __name__ == '__main__':
    unittest.main()
