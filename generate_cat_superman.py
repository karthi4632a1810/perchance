import requests
import urllib.parse

def generate_image():
    prompt = "A hyper-realistic professional photo of a cute fluffy cat flying through the clouds, wearing a detailed Superman suit with a flowing red cape, cinematic lighting, 8k resolution, highly detailed fur"
    encoded_prompt = urllib.parse.quote(prompt)
    
    # Trying a different public image generation service
    urls = [
        f"https://image.pollinations.ai/prompt/{encoded_prompt}",
        f"https://api.dice.global/image?prompt={encoded_prompt}"
    ]
    
    for url in urls:
        try:
            print(f"Attempting to generate image from: {url}")
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                with open("flying_cat_superman.jpg", "wb") as f:
                    f.write(response.content)
                print("Success! Image saved as flying_cat_superman.jpg")
                return True
            else:
                print(f"Service returned status code: {response.status_code}")
        except Exception as e:
            print(f"Request failed: {e}")
            
    print("All image generation attempts failed.")
    return False

if __name__ == "__main__":
    generate_image()
