from flask import Flask, request
import requests

TOKEN = "160966979:s3cnOPW18kZcUJRSpIUp8r68jnuvjUK72wQ"
BALE_API = f"https://tapi.bale.ai/bot{TOKEN}"

app = Flask(__name__)

def send_message(chat_id, text):
    url = BALE_API + "/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text
    }
    requests.post(url, json=data)

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text","")
        
        if text == "/start":
            send_message(chat_id,"به سامانه ثبت وصول مطالبات خوش آمدید")
        else:
            send_message(chat_id,"دستور نامعتبر")

    return "ok"

if __name__ == "__main__":
    app.run()
