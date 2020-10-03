from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import json
import time

with open('./setup.json') as fin:
    setup = json.load(fin)

driver = webdriver.Chrome(ChromeDriverManager().install())
driver.get("https://www.codewars.com/users/sign_in")

usernameElem = driver.find_element_by_id("user_email")
passwordElem = driver.find_element_by_id("user_password")

usernameElem.send_keys(setup['codewars']['email'])
passwordElem.send_keys(setup['codewars']['password'])

driver.find_element_by_xpath("//button[1]").click()

driver.find_element_by_xpath(
    "//div[contains(@class, 'profile-pic')]").click()
WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.LINK_TEXT, "Solutions")))
driver.find_element_by_link_text('Solutions').click()

nReloads = setup['reloads_in_browser']
elem = driver.find_element_by_tag_name("body")
for _ in range(nReloads):
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    # .build().perform()
    time.sleep(2)

with open('./source.html', 'w') as fin:
    fin.write(driver.page_source)

driver.close()
