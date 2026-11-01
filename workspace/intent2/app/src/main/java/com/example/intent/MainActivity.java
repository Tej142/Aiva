package com.example.intent;

import android.os.Bundle;
import android.widget.*;
import android.content.Intent;
import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.view.View;
import android.net.Uri;
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        Button bt = findViewById(R.id.btn);

        bt.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v){
                Intent o =new Intent(Intent.ACTION_DIAL,Uri.parse("tel:8639473796"));
                startActivity(o);
            }

        });
    }

}