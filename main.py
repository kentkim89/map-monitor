#!/usr/bin/env python3
"""
네이버 쇼핑 MAP 가격 모니터링 스크립트
쿠팡 로켓프레시 MAP 정책 위반 판매처 자동 감지 시스템
"""

import json
import time
import random
import logging
import requests
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import re
from urllib.parse import quote

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('map_monitor.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Product:
    """제품 정보 클래스"""
    brand: str
    name: str
    map_price: int
    search_keyword: str


@dataclass
class Violation:
    """MAP 위반 정보 클래스"""
    브랜드: str
    제품명: str
    쿠팡_MAP: int
    위반_업체명: str
    위반_가격: int
    위반_URL: str
    발견_시간: str


class Config:
    """설정 관리 클래스"""
    
    def __init__(self, config_path: str = 'config.json'):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
    
    @property
    def products(self) -> List[Product]:
        """제품 목록 반환"""
        products = []
        for brand_data in self.config['products']:
            brand = brand_data['brand']
            for product in brand_data['items']:
                products.append(Product(
                    brand=brand,
                    name=product['name'],
                    map_price=product['map_price'],
                    search_keyword=product.get('search_keyword', product['name'])
                ))
        return products
    
    @property
    def n8n_webhook_url(self) -> str:
        return self.config['n8n']['webhook_url']
    
    @property
    def crawler_delay(self) -> tuple:
        delay = self.config['crawler']['delay_range']
        return (delay['min'], delay['max'])
    
    @property
    def user_agent(self) -> str:
        return self.config['crawler']['user_agent']


class NaverShoppingCrawler:
    """네이버 쇼핑 크롤러 클래스"""
    
    def __init__(self, config: Config):
        self.config = config
        self.driver = None
        self.violations = []
        
    def setup_driver(self):
        """Selenium 드라이버 설정"""
        options = Options()
        options.add_argument(f'user-agent={self.config.user_agent}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        # 헤드리스 모드 옵션 (필요시 주석 해제)
        # options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
    def close_driver(self):
        """드라이버 종료"""
        if self.driver:
            self.driver.quit()
            
    def check_captcha(self) -> bool:
        """캡챠 체크"""
        try:
            # 네이버 캡챠 요소 확인
            captcha_elements = self.driver.find_elements(By.CLASS_NAME, 'captcha')
            if captcha_elements:
                logger.warning("⚠️ 캡챠 감지! 크롤링 중단")
                return True
                
            # reCAPTCHA iframe 확인
            recaptcha = self.driver.find_elements(By.XPATH, "//iframe[@title='reCAPTCHA']")
            if recaptcha:
                logger.warning("⚠️ reCAPTCHA 감지! 크롤링 중단")
                return True
                
        except Exception as e:
            logger.debug(f"캡챠 체크 중 오류: {e}")
            
        return False
    
    def random_delay(self):
        """랜덤 지연 시간 적용"""
        min_delay, max_delay = self.config.crawler_delay
        delay = random.uniform(min_delay, max_delay)
        logger.debug(f"대기 시간: {delay:.2f}초")
        time.sleep(delay)
        
    def extract_price(self, price_text: str) -> Optional[int]:
        """가격 텍스트에서 숫자 추출"""
        try:
            # 숫자만 추출
            price = re.sub(r'[^\d]', '', price_text)
            return int(price) if price else None
        except:
            return None
            
    def crawl_product(self, product: Product) -> List[Violation]:
        """개별 제품 크롤링"""
        violations = []
        
        try:
            # 네이버 쇼핑 검색 URL
            search_url = f"https://search.shopping.naver.com/search/all?query={quote(product.search_keyword)}"
            logger.info(f"🔍 검색 중: {product.name} ({product.brand})")
            
            self.driver.get(search_url)
            self.random_delay()
            
            # 캡챠 체크
            if self.check_captcha():
                raise Exception("캡챠 감지됨")
            
            # 검색 결과 대기
            wait = WebDriverWait(self.driver, 10)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "basicList_item__0T9JD")))
            
            # 스크롤하여 더 많은 상품 로드
            for _ in range(3):
                self.driver.execute_script("window.scrollBy(0, 1000)")
                time.sleep(1)
            
            # 상품 목록 크롤링
            items = self.driver.find_elements(By.CLASS_NAME, "basicList_item__0T9JD")
            
            for item in items:
                try:
                    # 브랜드 필터링 (고래미 또는 설래담 제품만)
                    title_elem = item.find_element(By.CLASS_NAME, "basicList_title__VfX3c")
                    title = title_elem.text
                    
                    if product.brand not in title:
                        continue
                    
                    # 판매처 정보
                    try:
                        mall_elem = item.find_element(By.CLASS_NAME, "basicList_mall__BC5Xu")
                        mall_name = mall_elem.text
                    except:
                        mall_name = "알 수 없음"
                    
                    # 가격 정보
                    try:
                        price_elem = item.find_element(By.CLASS_NAME, "price_num__S2p_v")
                        price = self.extract_price(price_elem.text)
                    except:
                        continue
                    
                    # URL 정보
                    try:
                        link_elem = item.find_element(By.CLASS_NAME, "basicList_link__JLQJf")
                        product_url = link_elem.get_attribute('href')
                    except:
                        product_url = ""
                    
                    # MAP 위반 체크
                    if price and price < product.map_price:
                        violation = Violation(
                            브랜드=product.brand,
                            제품명=product.name,
                            쿠팡_MAP=product.map_price,
                            위반_업체명=mall_name,
                            위반_가격=price,
                            위반_URL=product_url,
                            발견_시간=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        )
                        violations.append(violation)
                        logger.warning(f"⚠️ MAP 위반 발견: {mall_name} - {price:,}원 (MAP: {product.map_price:,}원)")
                    
                except Exception as e:
                    logger.debug(f"아이템 파싱 오류: {e}")
                    continue
                    
        except TimeoutException:
            logger.error(f"❌ 타임아웃: {product.name} 검색 결과를 찾을 수 없습니다")
        except Exception as e:
            logger.error(f"❌ 크롤링 오류 ({product.name}): {e}")
            
        return violations
    
    def crawl_all_products(self) -> List[Violation]:
        """모든 제품 크롤링"""
        all_violations = []
        
        try:
            self.setup_driver()
            
            for i, product in enumerate(self.config.products):
                logger.info(f"진행 상황: {i+1}/{len(self.config.products)}")
                
                violations = self.crawl_product(product)
                all_violations.extend(violations)
                
                # 마지막 제품이 아니면 추가 대기
                if i < len(self.config.products) - 1:
                    self.random_delay()
                    
        finally:
            self.close_driver()
            
        return all_violations


class N8NIntegration:
    """n8n 통합 클래스"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        
    def send_violations(self, violations: List[Violation]) -> bool:
        """위반 데이터를 n8n으로 전송"""
        if not violations:
            logger.info("✅ MAP 위반 사항 없음")
            return True
            
        try:
            # Violation 객체를 딕셔너리로 변환
            data = [asdict(v) for v in violations]
            
            # n8n Webhook으로 전송
            response = requests.post(
                self.webhook_url,
                json=data,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                logger.info(f"✅ n8n으로 {len(violations)}건의 위반 데이터 전송 완료")
                return True
            else:
                logger.error(f"❌ n8n 전송 실패: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ n8n 전송 오류: {e}")
            return False


def main():
    """메인 실행 함수"""
    try:
        # 설정 로드
        config = Config('config.json')
        logger.info("🚀 MAP 가격 모니터링 시작")
        
        # 크롤링 실행
        crawler = NaverShoppingCrawler(config)
        violations = crawler.crawl_all_products()
        
        # 결과 출력
        if violations:
            logger.info(f"⚠️ 총 {len(violations)}건의 MAP 위반 발견")
            
            # JSON 출력 (stdout)
            violations_json = json.dumps(
                [asdict(v) for v in violations],
                ensure_ascii=False,
                indent=2
            )
            print(violations_json)
            
            # n8n으로 전송
            if config.n8n_webhook_url:
                n8n = N8NIntegration(config.n8n_webhook_url)
                n8n.send_violations(violations)
                
            # 파일로 저장
            with open('violations.json', 'w', encoding='utf-8') as f:
                f.write(violations_json)
                
        else:
            logger.info("✅ MAP 위반 사항 없음")
            print("[]")  # 빈 JSON 배열 출력
            
    except FileNotFoundError:
        logger.error("❌ config.json 파일을 찾을 수 없습니다")
    except Exception as e:
        logger.error(f"❌ 실행 오류: {e}")
        raise


if __name__ == "__main__":
    main()
